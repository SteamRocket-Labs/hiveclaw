from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.database import PostgresTextContractError
from app.services.llm_client import LLMError


class _NoopStreamBatcher:
    def __init__(self, _send):
        pass

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_stream_persistence_failure_never_publishes_uncommitted_bytes():
    from app.services import web_chat_run_orchestrator as orchestrator

    broadcasts: list[dict] = []

    async def persist_stream_step(**_kwargs):
        return None

    async def broadcast(_agent_id, _session_id, payload):
        broadcasts.append(payload)

    state = SimpleNamespace(
        active_provider_request_id="provider-request-1",
        agent=SimpleNamespace(id=uuid4(), tenant_id=uuid4()),
        actor_user_id=uuid4(),
        actor_external_principal_id=None,
        session_id=str(uuid4()),
        run_uuid=uuid4(),
        ports=SimpleNamespace(
            events=SimpleNamespace(
                stream_batcher_type=_NoopStreamBatcher,
                persist_stream_step=persist_stream_step,
                broadcast=broadcast,
                build_chunk=lambda text, reset=False: {"type": "chunk", "content": text, "reset": reset},
                build_thinking=lambda text: {"type": "thinking", "content": text},
            )
        ),
    )

    callbacks = orchestrator._WebChatCallbacks(state)
    await callbacks.send_stream_event("chunk", "uncommitted model bytes")

    assert broadcasts == []


@pytest.mark.asyncio
async def test_raw_thinking_is_persisted_private_and_never_sent_to_direct_user():
    from app.services import web_chat_run_orchestrator as orchestrator

    persisted_calls: list[dict] = []
    broadcasts: list[dict] = []

    async def persist_stream_step(**kwargs):
        persisted_calls.append(kwargs)
        return {
            "schema": "hive.session_event",
            "schema_version": 2,
            "event_id": str(uuid4()),
            "sequence": 1,
            "visibility": {"audience": "private_provider"},
            "payload": {"content": kwargs["content"]},
        }

    async def broadcast(_agent_id, _session_id, payload):
        broadcasts.append(payload)

    state = SimpleNamespace(
        active_provider_request_id="provider-request-1",
        agent=SimpleNamespace(id=uuid4(), tenant_id=uuid4()),
        actor_user_id=uuid4(),
        actor_external_principal_id=None,
        session_id=str(uuid4()),
        run_uuid=uuid4(),
        ports=SimpleNamespace(
            events=SimpleNamespace(
                stream_batcher_type=_NoopStreamBatcher,
                persist_stream_step=persist_stream_step,
                broadcast=broadcast,
                build_chunk=lambda text, reset=False: {"type": "chunk", "content": text, "reset": reset},
                build_thinking=lambda text: {"type": "thinking", "content": text},
            )
        ),
    )

    callbacks = orchestrator._WebChatCallbacks(state)
    await callbacks.send_stream_event("thinking", "RAW_PRIVATE_REASONING")

    assert persisted_calls[0]["phase"] == "reasoning_private"
    assert broadcasts == []


@pytest.mark.asyncio
async def test_tool_callback_binds_provider_request_and_only_broadcasts_committed_canonical_envelopes():
    from app.services import web_chat_run_orchestrator as orchestrator

    persisted_calls: list[dict] = []
    broadcasts: list[dict] = []
    committed = {
        "schema": "hive.session_event",
        "schema_version": 2,
        "event_id": str(uuid4()),
        "sequence": 9,
        "visibility": {"audience": "direct_user"},
        "item_kind": "tool_call",
        "lifecycle": "started",
    }

    async def persist_tool_call(**kwargs):
        persisted_calls.append(kwargs)
        return [committed]

    async def broadcast(_agent_id, _session_id, payload):
        broadcasts.append(payload)

    state = SimpleNamespace(
        active_provider_request_id="provider-request-1",
        terminal_tool_card_finalized=False,
        phase_emitter=None,
        summary_turn_mode=False,
        stream_batcher=_NoopStreamBatcher(None),
        agent=SimpleNamespace(id=uuid4(), tenant_id=uuid4()),
        actor_user_id=uuid4(),
        actor_external_principal_id=None,
        session_id=str(uuid4()),
        run_uuid=uuid4(),
        ports=SimpleNamespace(
            events=SimpleNamespace(
                stream_batcher_type=_NoopStreamBatcher,
                tool_step_contract=lambda data, fallback_run_id=None: {
                    **data,
                    "runtime_task_id": str(fallback_run_id),
                },
                persist_tool_call=persist_tool_call,
                broadcast=broadcast,
            )
        ),
    )

    callbacks = orchestrator._WebChatCallbacks(state)
    await callbacks.tool_call(
        {
            "name": "read_file",
            "args": {"path": "README.md"},
            "status": "running",
            "tool_call_id": "provider-tool-1",
        }
    )

    assert persisted_calls[0]["data"]["provider_request_id"] == "provider-request-1"
    assert broadcasts == [committed]


@pytest.mark.parametrize(
    ("exc", "expected_reason"),
    [
        pytest.param(
            SQLAlchemyError("database unavailable"),
            "persistence_error",
            id="sqlalchemy-persistence-failure",
        ),
        pytest.param(
            PostgresTextContractError("invalid PostgreSQL text"),
            "persistence_error",
            id="postgres-text-contract-failure",
        ),
        pytest.param(
            LLMError("provider unavailable"),
            "provider_error",
            id="provider-failure",
        ),
        pytest.param(
            RuntimeError("unexpected runtime failure"),
            "turn_abort",
            id="non-provider-runtime-failure",
        ),
    ],
)
def test_runtime_exception_failure_uses_authoritative_error_class(
    exc,
    expected_reason: str,
):
    from app.services import web_chat_run_orchestrator as orchestrator

    failure = orchestrator._runtime_exception_failure(exc)

    assert failure.terminal_reason == expected_reason


@pytest.mark.asyncio
async def test_pre_invocation_finalization_preserves_full_response_summary():
    from app.services import web_chat_run_orchestrator as orchestrator

    captured: dict = {}

    async def finalize_with_assistant(**kwargs):
        captured.update(kwargs)
        return True

    async def emit_terminal_hook(**_kwargs):
        return None

    async def broadcast(*_args):
        return None

    state = SimpleNamespace(
        run_uuid=uuid4(),
        agent=SimpleNamespace(id=uuid4()),
        actor_user_id=uuid4(),
        session_id=str(uuid4()),
        metadata={},
        runtime_session_context=SimpleNamespace(source="web"),
        ports=SimpleNamespace(
            terminal=SimpleNamespace(
                finalize_with_assistant=finalize_with_assistant,
                emit_terminal_hook=emit_terminal_hook,
            ),
            events=SimpleNamespace(
                broadcast=broadcast,
                build_done=lambda response: {"type": "done", "response": response},
            ),
        ),
    )
    full_response = "plan response\n" + ("P" * 1000) + "\nEND_OF_PLAN_RESPONSE"

    await orchestrator._finalize_pre_invocation_response(
        state,
        full_response,
        status="completed",
        reason="invoke_complete",
    )

    assert captured["content"] == full_response
    assert captured["result_summary"] == full_response


@pytest.mark.asyncio
async def test_production_invocation_request_wires_session_v2_round_callbacks(monkeypatch):
    from app.services import session_model_round
    from app.services import web_chat_run_orchestrator as orchestrator

    tenant_id = uuid4()
    agent_id = uuid4()
    session_id = uuid4()
    run_id = uuid4()
    calls: list[tuple[str, dict]] = []

    class _DB:
        async def commit(self):
            calls.append(("db.commit", {}))

    class _TenantSession:
        async def __aenter__(self):
            return _DB()

        async def __aexit__(self, *_args):
            return False

    async def fake_bind(_db, **kwargs):
        calls.append(("bind", kwargs))
        return [{"role": "user", "content": "bound evidence"}]

    async def fake_prepare(_db, **kwargs):
        calls.append(("prepare", kwargs))
        return "provider-request-1"

    async def fake_seal(_db, **kwargs):
        calls.append(("response.sealed", kwargs))
        return {"result_id": str(uuid4())}

    async def fake_round_commit(_db, **kwargs):
        calls.append(("response.round_committed", kwargs))

    async def fake_fail(_db, **kwargs):
        calls.append(("failure", kwargs))

    monkeypatch.setattr(session_model_round, "bind_round_inputs", fake_bind)
    monkeypatch.setattr(session_model_round, "prepare_model_request", fake_prepare)
    monkeypatch.setattr(session_model_round, "seal_model_response", fake_seal)
    monkeypatch.setattr(session_model_round, "commit_sealed_model_round", fake_round_commit)
    monkeypatch.setattr(session_model_round, "fail_model_request", fake_fail)

    state = SimpleNamespace(
        llm_model=SimpleNamespace(provider="openai", model="gpt-4.1", supports_vision=False),
        fallback_model=None,
        conversation=[{"role": "user", "content": "initial"}],
        agent=SimpleNamespace(
            id=agent_id,
            tenant_id=tenant_id,
            name="Agent",
            role_description="Role",
        ),
        user=SimpleNamespace(username="owner", display_name="Owner"),
        actor_user_id=uuid4(),
        actor_external_principal_id=None,
        actor_authority_bound=False,
        runtime_session_context=SimpleNamespace(channel="web"),
        session_id=str(session_id),
        run_uuid=run_id,
        runtime_task=SimpleNamespace(claimed_by="worker-1", claim_version=3, attempt_count=2),
        cancel_event=SimpleNamespace(),
        metadata={"turn_id": "turn-live"},
        disable_tools_for_turn=False,
        excluded_tool_names_for_turn=(),
        ports=SimpleNamespace(runtime=SimpleNamespace(tenant_scoped_session=lambda _tenant_id: _TenantSession())),
    )
    callbacks = SimpleNamespace(stream=None, tool_call=None, thinking=None, runtime_event=None)
    request = orchestrator._agent_invocation_request(state, callbacks, "")

    assert await request.round_input_bind(2) == [{"role": "user", "content": "bound evidence"}]
    assert (
        await request.model_request_prepare(
            round_index=2,
            messages=[{"role": "user", "content": "bound evidence"}],
            tools=None,
            provider="openai",
            model="gpt-4.1",
        )
        == "provider-request-1"
    )
    await request.model_response_commit(
        round_index=2,
        provider_request_id="provider-request-1",
        response={"content_present": True},
    )
    await request.model_request_fail(
        round_index=2,
        provider_request_id="provider-request-1",
        error_class="LLMError",
        retry_safe=True,
    )

    assert [name for name, _ in calls if name != "db.commit"] == [
        "bind",
        "prepare",
        "response.sealed",
        "response.round_committed",
        "failure",
    ]
    assert calls[0][1]["run_id"] == run_id
    assert calls[0][1]["turn_id"] == "turn-live"
    assert calls[0][1]["round_index"] == 2


@pytest.mark.asyncio
async def test_ambiguous_provider_send_requires_reconciliation_without_assistant_prose():
    from app.kernel.contracts import ProviderRequestNeedsReconciliation
    from app.services import web_chat_run_orchestrator as orchestrator

    run_id = uuid4()
    agent_id = uuid4()
    calls: list[tuple[str, object]] = []

    async def update_runtime_task(_run_id, **payload):
        calls.append(("update", payload))

    async def finalize_with_assistant(**payload):
        calls.append(("finalize", payload))
        return True

    async def broadcast(_agent_id, _session_id, payload):
        calls.append(("broadcast", payload))

    state = SimpleNamespace(
        run_uuid=run_id,
        run_key=run_id.hex,
        agent=SimpleNamespace(id=agent_id),
        session_id=str(uuid4()),
        cancel_event=SimpleNamespace(is_set=lambda: False),
        terminal_phase_hint=None,
        ports=SimpleNamespace(
            runtime=SimpleNamespace(logger=SimpleNamespace(exception=lambda *_args: None)),
            terminal=SimpleNamespace(
                update_runtime_task=update_runtime_task,
                finalize_with_assistant=finalize_with_assistant,
            ),
            events=SimpleNamespace(broadcast=broadcast),
        ),
    )

    await orchestrator._handle_web_chat_failure(
        state,
        ProviderRequestNeedsReconciliation(
            provider_request_id="provider-request-ambiguous",
            error_class="timeout",
        ),
    )

    assert [name for name, _ in calls] == ["update", "broadcast"]
    assert calls[0][1]["status"] == "needs_reconciliation"
    assert calls[1][1] == {
        "type": "runtime_reconciliation_required",
        "run_id": str(run_id),
        "provider_request_id": "provider-request-ambiguous",
        "error_class": "timeout",
        "retryable": False,
    }


@pytest.mark.asyncio
async def test_model_text_that_looks_like_an_error_remains_typed_success(monkeypatch):
    from app.kernel.contracts import TerminalReason
    from app.services import web_chat_run_orchestrator as orchestrator

    captured: dict = {}

    async def capture_finalize(_state, _result, response, _thinking, status, metadata):
        captured.update(response=response, status=status, metadata=metadata)

    monkeypatch.setattr(orchestrator, "_finalize_assistant_response", capture_finalize)
    state = SimpleNamespace(
        interactive_pause_summary=None,
        plan_mode_submitted=False,
        runtime_session_context=SimpleNamespace(),
        thinking_content=[],
        cancel_event=SimpleNamespace(is_set=lambda: False),
        terminal_phase_hint=None,
        ports=SimpleNamespace(
            terminal=SimpleNamespace(
                plan_mode_terminal_error=lambda _context: None,
                clear_interactive_plan_mode=lambda _context: None,
                phase_for_status=lambda status: status,
                terminal_reason=lambda **payload: payload["status"],
            ),
            artifacts=SimpleNamespace(prompt_metadata=lambda _context: {}),
        ),
    )
    result = SimpleNamespace(
        content="Benign quoted text: [LLM Error] is an old UI label.",
        terminal_reason=TerminalReason.TURN_STOP,
        tokens_used=7,
    )

    await orchestrator._finalize_invocation_result(state, result)

    assert captured["status"] == "completed"
    assert captured["response"] == result.content


@pytest.mark.asyncio
async def test_committed_outcome_is_not_rewritten_when_sidecars_fail(monkeypatch):
    from app.services import web_chat_run_orchestrator as orchestrator

    warnings: list[tuple] = []
    legacy_calls: list[dict] = []

    async def committed(_state, _receipt):
        return True

    async def sidecar_failure(**_kwargs):
        raise PermissionError("sidecar unavailable")

    async def delivery_failure(*_args, **_kwargs):
        raise ConnectionError("socket closed")

    async def legacy_finalize(**kwargs):
        legacy_calls.append(kwargs)
        return True

    monkeypatch.setattr(orchestrator, "_commit_canonical_terminal_outcome", committed)
    state = SimpleNamespace(
        run_uuid=uuid4(),
        agent=SimpleNamespace(id=uuid4()),
        session_id=str(uuid4()),
        metadata={},
        runtime_session_context=SimpleNamespace(source="web"),
        ports=SimpleNamespace(
            terminal=SimpleNamespace(
                emit_terminal_hook=sidecar_failure,
                finalize_with_assistant=legacy_finalize,
            ),
            events=SimpleNamespace(
                broadcast=delivery_failure,
                build_done=lambda response, **_kwargs: {"type": "done", "response": response},
            ),
            artifacts=SimpleNamespace(),
            runtime=SimpleNamespace(
                logger=SimpleNamespace(warning=lambda *args: warnings.append(args)),
            ),
        ),
    )
    result = SimpleNamespace(
        model_result_receipt={"result_id": str(uuid4())},
        reasoning_signature=None,
    )

    await orchestrator._finalize_assistant_response(
        state,
        result,
        "exact model final bytes",
        None,
        "completed",
        {},
    )

    assert legacy_calls == []
    assert len(warnings) == 2
