"""R18a round 2 ruling 2(c) + round 3 ruling 3: business-task lane.

What each lane shows for a run stopped by the empty-search guard: the outcome
is FAILED (never NEEDS_RECONCILIATION); for failure_code repeated_empty_search
the member-facing summary is the plain sentence 「多次搜索都没有找到相关资料。」
and the outcome is not retryable (the member projection therefore offers no
retry); every other failure code — identical_terminal_task_read,
tool_round_limit, provider errors — keeps its English summary and retry. A
parent reading the business RuntimeTask through runtime_failure_observation
still sees the generic sentence (error_code sits under metadata.outcome;
pre-existing, report-only)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.services.test_task_executor import _FakeSession, _fake_resolve_tenant


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_code", ["repeated_empty_search", "identical_terminal_task_read"])
async def test_empty_search_guard_stop_is_a_plain_business_failure(monkeypatch, failure_code):
    from app.kernel import AgentKernel, InvocationRequest, KernelDependencies, RuntimeConfig
    from app.services.runtime_display_status import EMPTY_SEARCH_FINAL_MESSAGE
    from app.services import task_executor
    from app.services.task_executor import TaskExecutionStatus, execute_task

    task_id = uuid4()
    agent_id = uuid4()
    model_id = uuid4()
    creator_id = uuid4()
    plan_id = uuid4()

    task = SimpleNamespace(
        id=task_id, title="查公司知识库", description="找「白鹭计划」资料", type="todo", status="pending",
        completed_at=None, plan_id=plan_id, plan_version=1, plan_hash="sha256:task", plan_exempt_reason=None,
        plan_authorization={
            "schema": "hive.plan_authorization_evidence.v1",
            "lease_id": str(uuid4()),
            "canonical_args_hash": "args-hash",
            "target_ref": f"task:{task_id}:run",
            "requester_user_id": str(creator_id),
            "session_id": "task-session",
            "runtime_task_id": None,
            "evidence_id": f"task-run:{task_id}:request-1",
        },
    )
    tenant_id = uuid4()
    agent = SimpleNamespace(id=agent_id, name="Ops Agent", role_description="Operations",
                            primary_model_id=model_id, fallback_model_id=None,
                            creator_id=creator_id, tenant_id=tenant_id)
    model = SimpleNamespace(id=model_id, provider="openai", model="gpt-4.1", api_key="key",
                            base_url=None, max_output_tokens=None, tenant_id=tenant_id)
    sessions = [_FakeSession([task]), _FakeSession([agent, model, SimpleNamespace(
        id=creator_id, tenant_id=tenant_id, is_active=True), None]),
                _FakeSession([task])]

    async def fake_invoke_agent(request):
        """Run the REAL kernel loop with a model that keeps rewording an empty
        company-knowledge search; project the kernel result into the invoker
        result shape the executor consumes."""
        search_index = iter(range(1, 20))

        class RewordingClient:
            async def close(self) -> None:
                return None

            async def stream(self, **_kwargs):
                from app.services.llm_client import LLMResponse

                index = next(search_index)
                return LLMResponse(
                    content="",
                    tool_calls=[{"id": f"search-{index}", "type": "function", "function": {
                        "name": "search_company_kb",
                        "arguments": json.dumps({"query": f"白鹭计划 第{index}种说法"})}}],
                    finish_reason="tool_calls",
                    usage={"prompt_tokens": 10, "completion_tokens": 5},
                    model="gpt-4.1",
                )

        kernel = AgentKernel(KernelDependencies(
            resolve_runtime_config=lambda *_a, **_k: RuntimeConfig(tenant_id=tenant_id, max_tool_rounds=30),
            resolve_current_user_name=lambda *_a, **_k: "Ops Owner",
            build_system_prompt=lambda *_a, **_k: "PROMPT",
            resolve_memory_context=lambda *_a, **_k: "",
            get_tools=lambda *_a, **_k: [{
                "type": "function", "function": {
                    "name": "search_company_kb", "description": "", "parameters": {"type": "object"}}}],
            maybe_compress_messages=lambda messages, **_k: messages,
            create_client=lambda _m: RewordingClient(),
            execute_tool=lambda *_a, **_k: json.dumps({"status": "empty", "results": []}, ensure_ascii=False),
            persist_memory=lambda **_k: None,
            record_token_usage=lambda *_a, **_k: None,
            get_max_tokens=lambda *_a, **_k: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda n: n // 4,
        ))
        result = await kernel.handle(InvocationRequest(
            model=request.model, messages=[dict(message) for message in request.messages],
            agent_name=request.agent_name, role_description=request.role_description or "",
            agent_id=request.agent_id, user_id=request.user_id,
        ))
        # The kernel stop for repeated_empty_search is real; the
        # identical_terminal_task_read result shape is pinned by its own
        # kernel tests — here only the executor's mapping of the code varies.
        return SimpleNamespace(
            content=result.content,
            terminal_reason=getattr(result.terminal_reason, "value", result.terminal_reason),
            failure_code=failure_code,
            failure_delivery_state=result.failure_delivery_state,
            failure_retryable=result.failure_retryable,
            parts=list(result.parts or []),
            model_result_receipt=None,
        )

    async def fake_verify_authorization(**kwargs):
        return SimpleNamespace(lease_id=task.plan_authorization["lease_id"])

    async def fake_log_activity(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.services.task_executor.resolve_tenant_for_agent", _fake_resolve_tenant)
    monkeypatch.setattr("app.services.task_executor.tenant_scoped_session", lambda *a, **k: sessions.pop(0))
    monkeypatch.setattr("app.services.task_executor.TaskLog", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr("app.services.task_executor.invoke_agent", fake_invoke_agent)
    monkeypatch.setattr("app.services.task_executor._seal_task_t0_segment",
                        lambda **kwargs: None, raising=False)
    monkeypatch.setattr("app.services.task_executor.verify_consumed_plan_authorization_lease",
                        fake_verify_authorization)
    monkeypatch.setattr("app.services.activity_logger.log_activity", fake_log_activity)

    outcome = await execute_task(task_id, agent_id)

    # On the round-1 shape (TURN_STOP without a receipt) this was
    # NEEDS_RECONCILIATION; the guard's stop now ends as a plain failure.
    assert outcome.status is TaskExecutionStatus.FAILED
    assert outcome.error_code == failure_code
    assert outcome.terminal_reason == "loop_guard"

    if failure_code == "repeated_empty_search":
        # Ruling 3: the member summary is the plain sentence and the outcome is
        # not retryable — a retry would rerun the whole task.
        assert outcome.summary == EMPTY_SEARCH_FINAL_MESSAGE
        assert outcome.retryable is False
    else:
        # Every other code keeps its English summary and retry (green on the
        # round-2 commit too).
        assert outcome.summary == "Business task ended without a canonical completion (loop_guard)."
        assert outcome.retryable is True

    # The member projection derives can_retry from the recorded outcome.
    from app.services.business_task_runtime import project_business_task
    from datetime import datetime, timezone as tz

    runtime = SimpleNamespace(
        id=uuid4(), task_type="business_task", tenant_id=tenant_id,
        parent_agent_id=agent_id, status="failed",
        result_summary=outcome.summary, completed_at=datetime.now(tz.utc),
        claim_version=1, claim_expires_at=None, child_session_id=None,
        metadata_json={
            "business_task_id": str(task_id),
            "request_id": "runtime-request-r18a",
            "phase": "terminal",
            "outcome": outcome.to_dict(),
        },
    )
    projected_task = SimpleNamespace(
        id=task_id, tenant_id=tenant_id, agent_id=agent_id,
        active_runtime_task_id=runtime.id,
        status="failed", last_execution_status="failed",
        last_error=f"{failure_code}: {outcome.summary}" if not outcome.retryable else None,
        last_result=None, completed_at=None,
        plan_authorization={"lease_id": "lease-1"},
    )
    projection = project_business_task(task=projected_task, runtime_task=runtime)
    assert projection["recovery_message"] == outcome.summary
    assert projection["actions"]["can_retry"] is outcome.retryable
    if failure_code == "repeated_empty_search":
        assert projection["recovery_state"] != "retry_available"
