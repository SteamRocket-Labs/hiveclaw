"""R18a round 2: no lane is left waiting for an operator.

Rulings 1, 2(a) and 5 of the controller's round-2 rulings. Every test drives
the REAL web-chat orchestrator and REAL round ledger through
``execute_web_chat_run`` with only the provider client scripted, exactly like
the m1g49 harness: the ledger leaving the task ``running`` and the terminal
finalizer writing ``failed`` in a later transaction is the seam under test.
"""

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from tests.integration.test_web_chat_worker_restart_kernel_recovery import (
    ScriptedLLMClient,
    _fill_governance_allow_evidence,
    _seed_run,
)

pytestmark = [pytest.mark.usefixtures("migrated_pg_url")]

SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_company_kb",
        "description": "Search governed Company Knowledge publications.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}


def _patch_r18a_seams(monkeypatch, seed: dict, owner_sessionmaker, client) -> None:
    """The kernel-recovery seam set, with the knowledge search tool added.

    ``search_company_kb`` returns the gateway's typed empty result (the tool
    contract the loop guard reads); the gateway's own truthfulness is covered
    by the R17a tests in tests/tools/test_company_knowledge_tool.py."""
    import app.runtime.invoker as invoker
    import app.services.web_chat_runtime as runtime
    from app.database import tenant_scoped_session
    from app.kernel import RuntimeConfig

    async def _empty(*args, **kwargs):
        return ""

    async def _resolve_tenant(_agent_id, **_kwargs):
        return seed["tenant_id"]

    @asynccontextmanager
    async def _tenant_session(_tenant_id=None, **_kwargs):
        async with tenant_scoped_session(_tenant_id, session_factory=owner_sessionmaker, require_tenant=True) as db:
            yield db

    monkeypatch.setattr(runtime, "_async_session", owner_sessionmaker)
    monkeypatch.setattr(runtime, "tenant_scoped_session", _tenant_session)
    monkeypatch.setattr("app.services.tenant_resolver.resolve_tenant_for_agent", _resolve_tenant)
    monkeypatch.setattr(
        runtime, "get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(seed["data_root"])),
        raising=False,
    )

    async def _resolve_runtime_config(_agent_id):
        return RuntimeConfig(tenant_id=seed["tenant_id"], max_tool_rounds=30)

    async def _execute_tool(
        tool_name, args, *rest, tool_call_id=None, trace_metadata_sink=None, pre_effect_callback=None, **kwargs
    ):
        if pre_effect_callback is not None:
            await pre_effect_callback(
                {"tool_call_id": tool_call_id, "tool_name": tool_name, "arguments": dict(args)}
            )
        result = json.dumps({"status": "empty", "results": []}, ensure_ascii=False)
        _fill_governance_allow_evidence(
            trace_metadata_sink, tool_name=tool_name, args=dict(args), result=result
        )
        return result

    monkeypatch.setattr(invoker, "build_agent_context", _empty, raising=False)
    monkeypatch.setattr(invoker, "build_agent_runtime_context", _empty, raising=False)
    monkeypatch.setattr(invoker, "build_memory_context", _empty, raising=False)
    monkeypatch.setattr(invoker, "resolve_retrieval_context", _empty, raising=False)
    monkeypatch.setattr(invoker, "_resolve_runtime_config", _resolve_runtime_config, raising=False)
    monkeypatch.setattr(invoker, "maybe_compress_messages", lambda messages, **kwargs: messages, raising=False)
    monkeypatch.setattr(invoker, "get_agent_tools_for_llm", lambda *args, **kwargs: [SEARCH_TOOL], raising=False)
    monkeypatch.setattr(invoker, "execute_tool", _execute_tool, raising=False)
    monkeypatch.setattr(invoker, "create_llm_client", lambda **kwargs: client, raising=False)
    monkeypatch.setattr(invoker, "record_token_usage", lambda *args, **kwargs: None, raising=False)
    monkeypatch.setattr(invoker, "record_invocation_span", lambda *args, **kwargs: None, raising=False)
    monkeypatch.setattr(invoker, "persist_runtime_memory", lambda *args, **kwargs: None, raising=False)
    monkeypatch.setattr(invoker, "get_max_tokens", lambda *args, **kwargs: 2048, raising=False)

    async def _quota_allowed(*args, **kwargs):
        return None

    monkeypatch.setattr(invoker, "check_user_token_quota", _quota_allowed, raising=False)
    monkeypatch.setattr(invoker, "async_session", owner_sessionmaker, raising=False)

    @asynccontextmanager
    async def _invoker_tenant_session(_tenant_id=None, **_kwargs):
        async with owner_sessionmaker() as db:
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    monkeypatch.setattr(invoker, "tenant_scoped_session", _invoker_tenant_session, raising=False)


async def _execute_claimed_run(owner_sessionmaker, seed: dict, *, claim_version: int = 1):
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.web_chat_runtime import execute_web_chat_run

    async with owner_sessionmaker() as db:
        from app.models.runtime_task import RuntimeTask

        run = await db.get(RuntimeTask, seed["run_id"])
        run.status, run.claimed_by = "running", "r18a-worker"
        run.claim_version = claim_version
        run.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        await db.commit()
    token = set_runtime_task_fence(task_id=seed["run_id"], worker_id="r18a-worker", claim_version=claim_version)
    try:
        await execute_web_chat_run(seed["run_id"])
    finally:
        reset_runtime_task_fence(token)


async def _failure_events(owner_sessionmaker, session_id) -> list[dict]:
    from app.models.chat_transcript_event import ChatTranscriptEvent

    async with owner_sessionmaker() as db:
        rows = (
            await db.execute(
                select(ChatTranscriptEvent).where(
                    ChatTranscriptEvent.session_id == session_id,
                    ChatTranscriptEvent.event_type == "runtime_failure.recorded",
                )
            )
        ).scalars().all()
    payloads = []
    for row in rows:
        metadata = dict(row.metadata_json or {})
        payloads.append(dict(metadata.get("v2_payload") or {}))
    return payloads


async def _admit_and_run_next_message(owner_sessionmaker, monkeypatch, seed: dict, client) -> str:
    """The next message in the same session must be accepted and run."""
    from app.models.runtime_task import RuntimeTask
    from app.services.session_human_input import queue_admitted_human_input
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_v2_persistence import accept_human_input, resolve_session_mutation_authority

    from app.services.session_input_dispatch import dispatch_admitted_input_fast_path

    async with owner_sessionmaker() as db:
        from app.models.user import User

        user = await db.get(User, seed["user_id"])
        authority = await resolve_session_mutation_authority(
            db, user=user, agent_id=seed["agent_id"], session_id=seed["session_id"],
            action="mutate_session_input",
        )
        input_id = uuid.uuid4()
        await accept_human_input(
            db, authority=authority,
            intent={
                "kind": "steer_current_turn",
                "input_id": str(input_id),
                "idempotency_key": f"input:{input_id}",
                "session_id": str(seed["session_id"]),
                "content_parts": [{"type": "text", "text": "那就帮我总结一下今天做了什么"}],
                "expected_turn_id": None,
                "expected_run_id": None,
                "terminal_fallback": "queue_next_turn",
            },
        )
        outcome = await run_user_prompt_admission(
            db, authority=authority, input_id=input_id, worker_id="r18a-worker-2"
        )
        # R18a round 3 (Grok nit 3): the next input is genuinely admitted…
        assert outcome.state == "admitted", outcome
        await queue_admitted_human_input(db, authority=authority, input_id=input_id)
        # …and the real dispatch lane creates the successor run for it.
        dispatch = await dispatch_admitted_input_fast_path(
            db, admission_id=outcome.admission_id, worker_id="r18a-worker-2"
        )
        await db.commit()
    if dispatch.state == "pending":
        # The durable dispatch lane correctly waits until the failed run's
        # terminal boundary has been delivered; drain the real outbox lane and
        # let the dispatch run again.
        from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
        from app.services.web_terminal_boundary_processor import validate_web_terminal_boundary

        service = RuntimeTerminalBoundaryOutboxService(session_factory=owner_sessionmaker)

        async def _deliver(item):
            return {"source_ref": f"runtime-terminal-boundary://{item.id}"}

        await service.drain_once(
            tenant_id=seed["tenant_id"], worker_id="r18a-worker-2",
            canonical_validator=validate_web_terminal_boundary, process_callback=_deliver,
        )
        async with owner_sessionmaker() as db:
            dispatch = await dispatch_admitted_input_fast_path(
                db, admission_id=outcome.admission_id, worker_id="r18a-worker-2"
            )
            await db.commit()
    assert dispatch.state == "dispatched", dispatch
    from sqlalchemy import or_

    async with owner_sessionmaker() as db:
        runs = (await db.execute(
            select(RuntimeTask).where(
                RuntimeTask.parent_session_id == str(seed["session_id"]),
                or_(
                    RuntimeTask.metadata_json["session_v2_input_id"].as_string() == str(input_id),
                    RuntimeTask.metadata_json["session_v2_rolled_over_input_id"].as_string() == str(input_id),
                ),
            ))).scalars().all()
    assert len(runs) == 1, [str(run.id) for run in runs]
    next_run_id = runs[0].id
    next_seed = {**seed, "run_id": next_run_id}
    await _execute_claimed_run(owner_sessionmaker, next_seed, claim_version=0)
    return str(next_run_id)


async def _run_status(owner_sessionmaker, run_id) -> tuple[str, dict]:
    from app.models.runtime_task import RuntimeTask

    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, run_id)
        return run.status, dict(run.metadata_json or {})


async def test_ruling5_effect_free_timeout_settles_failed_and_session_continues(
    owner_sessionmaker, monkeypatch, tmp_path
):
    """Ruling 5: timeout with unknown delivery after a committed read-only
    tool — round failed, run failed, no blocker, the ruling-4 card fields,
    and the next message in the same session accepted and run."""
    from app.models.session_v2 import SessionModelResult
    from app.services.llm_client import LLMError
    from app.services.session_control_plane import _runtime_task_user_blocker
    from app.models.runtime_task import RuntimeTask

    seed = await _seed_run(
        owner_sessionmaker, tmp_path, prompt="查一下公司知识库里有没有关于「白鹭计划」的资料"
    )
    client = ScriptedLLMClient([
        {"tool_calls": [{"id": "search-1", "type": "function", "function": {
            "name": "search_company_kb", "arguments": json.dumps({"query": "白鹭计划"})}}],
         "finish_reason": "tool_calls"},
        LLMError("Provider stream delivery is unknown: ReadTimeout: timed out"),
    ])
    _patch_r18a_seams(monkeypatch, seed, owner_sessionmaker, client)
    await _execute_claimed_run(owner_sessionmaker, seed)

    status, metadata = await _run_status(owner_sessionmaker, seed["run_id"])
    assert status == "failed"
    assert "session_v2_reconciliation" not in metadata  # no operator hold anywhere
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, seed["run_id"])
        assert _runtime_task_user_blocker(run) is None
        rounds = (await db.execute(
            select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"]))).scalars().all()
    states = {row.round_id.split(":round:")[1]: row.state for row in rounds}
    assert states == {"1": "round_committed", "2": "failed"}  # committed tool, then a plain failed round

    failures = await _failure_events(owner_sessionmaker, seed["session_id"])
    assert len(failures) == 1
    payload = failures[0]
    assert payload["failure_code"] == "timeout"
    assert payload["delivery_state"] == "unknown"
    assert payload["retry_safe"] is False  # these fields drive the ruling-4 sentence and no retry action

    next_client = ScriptedLLMClient([
        {"content": "今天主要在整理知识库相关资料。", "finish_reason": "stop"},
    ])
    _patch_r18a_seams(monkeypatch, seed, owner_sessionmaker, next_client)
    next_run_id = await _admit_and_run_next_message(owner_sessionmaker, monkeypatch, seed, next_client)
    next_status, _ = await _run_status(owner_sessionmaker, next_run_id)
    assert next_status == "completed"  # the session took the next message and ran it


async def test_ruling2a_empty_search_stop_completes_the_web_run_with_an_assistant_answer(
    owner_sessionmaker, monkeypatch, tmp_path
):
    """R20b/P2: nothing found is a completed answer, with no operator hold."""
    seed = await _seed_run(
        owner_sessionmaker, tmp_path, prompt="查一下公司知识库里有没有关于「白鹭计划」的资料，没有的话直接告诉我"
    )
    answer = "公司知识库里多次搜索都没有找到关于「白鹭计划」的相关资料。"
    client = ScriptedLLMClient([
        *[
        {"tool_calls": [{"id": f"search-{index}", "type": "function", "function": {
            "name": "search_company_kb", "arguments": json.dumps({"query": f"白鹭计划 第{index}种说法"})}}],
         "finish_reason": "tool_calls"}
        for index in range(1, 6)
        ],
        {"content": answer, "finish_reason": "stop"},
    ])
    _patch_r18a_seams(monkeypatch, seed, owner_sessionmaker, client)
    await _execute_claimed_run(owner_sessionmaker, seed)

    status, metadata = await _run_status(owner_sessionmaker, seed["run_id"])
    # R20b/P2 supersedes the web failure card: persist the normal model answer.
    assert status == "completed"
    assert not metadata.get("error_code")
    assert not metadata.get("failure_code")
    assert "session_v2_reconciliation" not in metadata

    failures = await _failure_events(owner_sessionmaker, seed["session_id"])
    assert failures == []

    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionModelResult

    async with owner_sessionmaker() as db:
        replies = list(await db.scalars(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.run_id == seed["run_id"],
            ChatTranscriptEvent.event_type == "assistant_text.snapshot")))
        assert [reply.content for reply in replies] == [answer]
        final_round = await db.scalar(select(SessionModelResult).where(
            SessionModelResult.run_id == seed["run_id"],
            SessionModelResult.round_id == f"{seed['run_id']}:round:6"))
        assert final_round is not None and final_round.state == "round_committed"
        assert replies[0].result_id == final_round.id
        finals = list(await db.scalars(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.run_id == seed["run_id"],
            ChatTranscriptEvent.event_type == "assistant_final.completed")))
        assert len(finals) == 1 and finals[0].result_id == final_round.id
        reconciliations = (await db.execute(
            select(ChatTranscriptEvent.event_type).where(
                ChatTranscriptEvent.session_id == seed["session_id"],
                ChatTranscriptEvent.event_type == "run.needs_reconciliation"))).scalars().all()
    assert reconciliations == []
    assert len(client.requests) == 6 and client.requests[-1]["tools"] is None

    next_client = ScriptedLLMClient([{"content": "好的，有别的需要随时找我。", "finish_reason": "stop"}])
    _patch_r18a_seams(monkeypatch, seed, owner_sessionmaker, next_client)
    next_run_id = await _admit_and_run_next_message(owner_sessionmaker, monkeypatch, seed, next_client)
    next_status, _ = await _run_status(owner_sessionmaker, next_run_id)
    assert next_status == "completed"
    assert any(message["role"] == "assistant" and answer in str(message["content"])
               for message in next_client.requests[0]["messages"])


async def test_ruling1_effect_free_timeout_never_reaches_the_fallback_model(
    owner_sessionmaker, monkeypatch, tmp_path
):
    """Ruling 1: with a fallback model configured, the ledger-settled failure
    exits with the classified provider error; the fallback client is never
    called and only one round is ever prepared."""
    from app.models.agent import Agent
    from app.models.llm import LLMModel
    from app.models.session_v2 import SessionModelResult
    from app.services.llm_client import LLMError

    seed = await _seed_run(owner_sessionmaker, tmp_path, prompt="继续处理")
    async with owner_sessionmaker() as db:
        fallback_model_id = uuid4()
        db.add(LLMModel(id=fallback_model_id, tenant_id=seed["tenant_id"], provider="anthropic",
                        model="fallback-sonnet", api_key_encrypted="local-fake-provider-only",
                        label="Fallback", enabled=True))
        await db.flush()
        agent = await db.get(Agent, seed["agent_id"])
        agent.fallback_model_id = fallback_model_id
        await db.commit()

    primary = ScriptedLLMClient([LLMError("Provider stream delivery is unknown: ReadTimeout: timed out")])
    fallback_client = ScriptedLLMClient([])

    import app.runtime.invoker as invoker
    import app.services.web_chat_runtime as runtime

    _patch_r18a_seams(monkeypatch, seed, owner_sessionmaker, primary)

    def _client_factory(**kwargs):
        return primary if kwargs.get("provider") == "openai" else fallback_client

    monkeypatch.setattr(invoker, "create_llm_client", _client_factory, raising=False)
    broadcasts: list[dict] = []

    async def _record_broadcast(_agent_id, _session_id, event):
        broadcasts.append(dict(event))

    monkeypatch.setattr(runtime, "broadcast_web_chat_event", _record_broadcast)
    await _execute_claimed_run(owner_sessionmaker, seed)

    assert len(primary.requests) == 1
    assert fallback_client.requests == []  # no fallback activation
    # R18a round 3 (reviewer nit c): no "Fallback Model Activated" event either.
    assert all(event.get("type") != "runtime_fallback" for event in broadcasts)
    status, metadata = await _run_status(owner_sessionmaker, seed["run_id"])
    assert status == "failed"
    assert metadata.get("error_code") == "timeout"
    async with owner_sessionmaker() as db:
        rounds = (await db.execute(
            select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"]))).scalars().all()
    assert [row.round_id.split(":round:")[1] for row in rounds] == ["1"]  # the same round was never re-prepared
    assert rounds[0].state == "failed"
    assert rounds[0].state != "needs_reconciliation"


async def test_ruling2b_delegated_colleague_completes_and_parent_reads_the_sentence(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path
):
    """R20b/P2: a committed summary settles the child and its parent return."""
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult
    from app.services.runtime_display_status import runtime_failure_observation
    from app.services.session_control_plane import _runtime_task_user_blocker
    from tests.integration.test_r20b_empty_search_summary import ANSWER, _run_delegated_empty_search_summary

    case = await _run_delegated_empty_search_summary(
        owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    seed, result = case["seed"], case["result"]

    # R20b/P2: displayed state matches the summary; its receipt settles the lane (R18a round 2 blocker 2).
    assert result.failed is False
    # R20b/P2: the summary now settles the lane normally (R18a round 2 blocker 2).
    assert result.terminal_reason == "turn_stop"
    # R20b/P2: completed summary work carries no failure code (R18a round 2 blocker 2).
    assert result.failure_metadata is None
    status, metadata = await _run_status(owner_sessionmaker, seed["run_id"])
    # R20b/P2: the persisted state matches the summary completion (R18a round 2 blocker 2).
    assert status == "completed"
    assert not metadata.get("failure_code") and not metadata.get("error_code")
    assert "session_v2_reconciliation" not in metadata

    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, seed["run_id"])
        assert _runtime_task_user_blocker(run) is None
        summary = await db.scalar(select(SessionModelResult).where(
            SessionModelResult.run_id == run.id,
            SessionModelResult.round_id == f"{run.id}:round:6"))
        assert summary is not None and summary.state == "round_committed"
        assert summary.seal_json["response"]["content"] == ANSWER
    assert len(case["client"].requests) == 6 and case["client"].requests[-1]["tools"] is None
    assert result.response_complete_payload and result.transcript_committed
    # R20b/P2: the normal answer replaces the failure observation; its receipt settles the lane (R18a round 2 blocker 2).
    assert runtime_failure_observation(status, metadata) is None
    # R20b/P2: the parent receives the completed summary answer (R18a round 2 blocker 2).
    assert result.content == ANSWER
    assert ANSWER in json.dumps(case["parent_client"].requests[1]["messages"], ensure_ascii=False)
