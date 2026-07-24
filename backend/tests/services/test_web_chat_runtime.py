from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.services.llm_client import LLMError


async def _noop_async(*_args, **_kwargs):
    return None


def test_committed_turn_usage_restores_logical_rounds_without_counting_continuations_twice():
    from app.services.web_chat_runtime import _committed_turn_usage_tokens

    run_id = uuid4()
    rows = [
        SimpleNamespace(
            round_id=f"{run_id}:round:1",
            seal_json={"continuation_index": 0, "usage": {"total_tokens": 20}, "response": {}},
            model_request_snapshot_json={"continuation_index": 0, "wire_request": {"messages": []}},
        ),
        SimpleNamespace(
            round_id=f"{run_id}:round:1:output-continuation:1",
            seal_json={"continuation_index": 1, "usage": {"total_tokens": 5}, "response": {}},
            model_request_snapshot_json={"continuation_index": 1, "wire_request": {"messages": []}},
        ),
        SimpleNamespace(
            round_id=f"{run_id}:round:2",
            seal_json={
                "continuation_index": 0,
                "usage": {
                    "total_tokens": 100,
                    "prompt_tokens_details": {"cached_tokens": 80},
                },
                "response": {},
            },
            model_request_snapshot_json={"continuation_index": 0, "wire_request": {"messages": []}},
        ),
        SimpleNamespace(
            round_id=f"{run_id}:round:3",
            seal_json={"continuation_index": 0, "usage": {"total_tokens": 99}, "response": {}},
            model_request_snapshot_json={"continuation_index": 0, "wire_request": {"messages": []}},
        ),
    ]

    assert _committed_turn_usage_tokens(rows, resume_round_index=2) == 40


def test_committed_turn_usage_preserves_trusted_zero_and_estimates_only_missing_usage():
    from app.services.web_chat_runtime import _committed_turn_usage_tokens

    run_id = uuid4()
    rows = [
        SimpleNamespace(
            round_id=f"{run_id}:round:1",
            seal_json={
                "continuation_index": 0,
                "usage": {"total_tokens": 100, "cached_tokens": 100},
                "response": {"content": ""},
            },
            model_request_snapshot_json={
                "continuation_index": 0,
                "wire_request": {"messages": [{"role": "user", "content": "cached request"}]},
            },
        ),
        SimpleNamespace(
            round_id=f"{run_id}:round:2",
            seal_json={"continuation_index": 0, "usage": {}, "response": {"content": "1234567"}},
            model_request_snapshot_json={
                "continuation_index": 0,
                "wire_request": {"messages": [{"role": "user", "content": "12345678901234"}]},
            },
        ),
    ]

    assert _committed_turn_usage_tokens(rows, resume_round_index=2) == 6


async def _fake_canonical_active_input(**kwargs):
    active = kwargs["active_run"]
    input_id = uuid4()
    content = kwargs["content"]
    display_content = kwargs.get("display_content") or content
    file_name = kwargs.get("file_name") or ""
    saved_content = f"[file:{file_name}]\n{display_content}" if file_name else display_content
    queued = {
        "id": str(input_id),
        "input_id": str(input_id),
        "content": saved_content,
        "llm_content": content,
        "display_content": display_content,
        "role": kwargs.get("role") or "user",
        "source": kwargs.get("source_channel") or "web",
        "file_name": file_name,
        "attachments": kwargs.get("attachments") or [],
        "parts": kwargs.get("parts") or [],
        "status": "queued",
        "dispatch_status": "mailbox_queued",
        "queue_ordinal": 1,
    }
    return {
        "run_id": active.id.hex,
        "status": active.status,
        "turn_id": (active.metadata_json or {}).get("turn_id") or f"turn-{active.id.hex}",
        "queued": queued,
        "queued_user_message": queued,
        "session_input_receipt": {"input_id": str(input_id), "input_status": "queued"},
    }


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return SimpleNamespace(first=lambda: self._value, all=lambda: [self._value] if self._value else [])


class _FakeDB:
    def __init__(self, active_run=None):
        self.active_run = active_run
        self.added = []
        self.commits = 0

    async def execute(self, _stmt):
        if "session_writer_epochs" in str(_stmt):
            return _ScalarResult(
                SimpleNamespace(
                    state="legacy_open",
                    new_run_generation=1,
                    allowed_existing_generations_json=[1],
                    enforcement_mode="observe",
                    version=1,
                    release_id="test-epoch",
                )
            )
        return _ScalarResult(self.active_run)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commits += 1

    async def flush(self):
        return None


def test_public_tool_step_contract_never_carries_provider_private_reasoning():
    import app.services.web_chat_runtime as runtime

    payload = runtime._tool_step_contract(
        {
            "name": "read_file",
            "status": "done",
            "tool_call_id": "provider-tool-1",
            "result": "public tool result",
            "reasoning_content": "RAW_PRIVATE_REASONING",
            "reasoning_signature": "provider-private-signature",
        }
    )

    assert "reasoning_content" not in payload
    assert "reasoning_signature" not in payload
    assert payload["result"] == "public tool result"


def test_clear_interactive_plan_mode_clears_typed_state_and_metadata_mirror():
    import app.services.web_chat_runtime as runtime
    from app.runtime.session import PlanModeState, SessionContext

    context = SessionContext(session_id="session-1", source="web_chat", channel="web")
    context.plan_mode = PlanModeState(
        active=True,
        original_request="schedule daily brief",
        intent_type="autonomous_wake",
        action_kind="create_enabled_trigger",
        tool_name="set_trigger",
        plan_file_path="workspace/plans/session-1.plan.md",
    )
    context.metadata["plan_mode"] = context.plan_mode.to_metadata()

    runtime._clear_interactive_plan_mode(context)

    assert context.plan_mode.active is False
    assert context.plan_mode.original_request is None
    assert "plan_mode" not in context.metadata


def test_cc_session_task_types_are_executable_chat_runs():
    import app.services.web_chat_runtime as runtime

    assert runtime.is_executable_chat_task_type("web_chat_turn")
    assert runtime.is_executable_chat_task_type("goal_continuation")
    assert runtime.is_executable_chat_task_type("team_member")
    assert runtime.is_executable_chat_task_type("advanced_plan")
    assert not runtime.is_executable_chat_task_type("delegation")


def test_history_replay_preserves_full_unfrozen_tool_result():
    import app.services.web_chat_runtime as runtime

    full_result = "tool evidence\n" + ("R" * 60000) + "\nEND_OF_TOOL_EVIDENCE"
    history = [
        SimpleNamespace(
            id=uuid4(),
            role="tool_call",
            content=json.dumps(
                {
                    "name": "read_file",
                    "args": {"path": "large.txt"},
                    "result": full_result,
                    "tool_call_id": "call-full-result",
                }
            ),
        )
    ]

    conversation = runtime.conversation_from_history_messages(history)

    assert conversation[-1] == {
        "role": "tool",
        "tool_call_id": "call-full-result",
        "content": full_result,
    }


def test_terminal_status_never_comes_from_assistant_natural_language_prefix():
    import app.services.web_chat_runtime as runtime

    event = SimpleNamespace(
        event_type="assistant_message",
        metadata_json={},
        content="[LLM Error] This is model-authored analysis of an error format.",
    )

    assert runtime._terminal_status_from_transcript_event(event) == "completed"


def test_quota_unavailable_is_a_failed_terminal_event_with_preserved_reason():
    import app.services.web_chat_runtime as runtime
    from app.kernel.contracts import TerminalReason

    event = SimpleNamespace(
        event_type="quota_exceeded",
        metadata_json={"status": "unavailable", "retryable": True},
        content="Token quota could not be verified.",
    )

    assert runtime._terminal_status_from_transcript_event(event) == "failed"
    assert (
        runtime._terminal_reason_value_for_web_run(
            status="failed",
            result_reason=TerminalReason.QUOTA_UNAVAILABLE,
        )
        == TerminalReason.QUOTA_UNAVAILABLE.value
    )


def test_terminal_task_update_persists_and_projects_terminal_reason():
    import app.services.web_chat_runtime as runtime
    from app.kernel.contracts import TerminalReason

    task = SimpleNamespace(
        id=uuid4(),
        status="running",
        created_at=None,
        started_at=None,
        completed_at=None,
        result_summary=None,
        metadata_json={"turn_id": "turn-1"},
    )

    runtime._apply_terminal_task_update(
        task,
        status="failed",
        result_summary="provider failed",
        metadata_json={"terminal_reason": TerminalReason.PROVIDER_ERROR.value},
    )
    payload = runtime._runtime_task_to_run(task)

    assert task.metadata_json["terminal_reason"] == "provider_error"
    assert payload["terminal_reason"] == "provider_error"


def test_terminal_task_update_preserves_existing_killed_status():
    import app.services.web_chat_runtime as runtime

    task = SimpleNamespace(
        id=uuid4(),
        status="killed",
        created_at=None,
        started_at=None,
        completed_at=datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc),
        result_summary="cancelled by user",
        metadata_json={"cancelled_by": "user"},
    )

    runtime._apply_terminal_task_update(
        task,
        status="completed",
        result_summary="final answer completed",
        metadata_json={"terminal_reason": "stop"},
    )

    assert task.status == "killed"
    assert task.result_summary == "cancelled by user"
    assert task.metadata_json["terminal_reason"] == "stop"
    assert task.metadata_json["terminal_update_preserved_status"] == "killed"
    assert task.metadata_json["terminal_update_attempted_status"] == "completed"


def test_terminal_task_update_preserves_existing_completed_status_from_late_kill():
    import app.services.web_chat_runtime as runtime

    task = SimpleNamespace(
        id=uuid4(),
        status="completed",
        created_at=None,
        started_at=None,
        completed_at=datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc),
        result_summary="model-authored final",
        metadata_json={"terminal_reason": "stop"},
    )

    runtime._apply_terminal_task_update(
        task,
        status="killed",
        result_summary="late cancellation",
        metadata_json={"cancelled_by": "late_control"},
    )

    assert task.status == "completed"
    assert task.result_summary == "model-authored final"
    assert task.metadata_json["terminal_update_preserved_status"] == "completed"
    assert task.metadata_json["terminal_update_attempted_status"] == "killed"


def test_runtime_session_permission_metadata_prefers_latest_session_override():
    import app.services.web_chat_runtime as runtime
    from app.runtime.session import SessionContext

    context = SessionContext(session_id="session-1", source="web", channel="web")

    merged = runtime._merge_runtime_permission_metadata(
        runtime_metadata={
            "permission_mode": "default",
            "writable_roots": ["workspace/"],
            "permission_profile": {"mode": "default", "allowed_tools": [], "writable_roots": ["workspace/"]},
        },
        session_metadata={
            "permission_mode": "bypassPermissions",
            "permission_profile": {"mode": "bypassPermissions", "allowed_tools": ["track_todo"]},
        },
    )
    runtime._sync_runtime_session_permission_metadata(context, merged)

    assert merged["permission_mode"] == "bypassPermissions"
    assert merged["writable_roots"] == ["workspace/"]
    assert merged["permission_profile"] == {
        "mode": "bypassPermissions",
        "allowed_tools": ["track_todo"],
        "writable_roots": ["workspace/"],
        "session_grants": [],
    }
    assert context.metadata["permission_mode"] == "bypassPermissions"
    assert context.metadata["writable_roots"] == ["workspace/"]
    assert context.metadata["permission_profile"] == {
        "mode": "bypassPermissions",
        "allowed_tools": ["track_todo"],
        "writable_roots": ["workspace/"],
        "session_grants": [],
    }


@pytest.mark.asyncio
async def test_execute_web_chat_run_keeps_channel_delivery_tools_visible_for_web_turn(monkeypatch):
    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    runtime_task = SimpleNamespace(
        id=run_id,
        parent_agent_id=agent_id,
        parent_session_id=session_id,
        prompt="web follow-up",
        metadata_json={
            "user_id": str(user_id),
            "session_id": session_id,
            "source": "web",
            "allow_channel_delivery_tools": True,
        },
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="Agent",
        role_description="",
        primary_model_id=uuid4(),
        fallback_model_id=None,
        tenant_id=uuid4(),
        agent_type="native",
    )
    user = SimpleNamespace(id=user_id, display_name="Rocky", username="rocky")
    llm_model = SimpleNamespace(provider="openai", model="gpt-4.1", supports_vision=False)
    captured = {}

    async def fake_load_context(_run_uuid):
        return (
            runtime_task,
            agent,
            user,
            llm_model,
            None,
            [],
            SimpleNamespace(delivery_target_json={"channel": "feishu"}),
        )

    async def fake_invoke(request):
        captured["excluded_tool_names"] = request.excluded_tool_names
        captured["system_prompt_suffix"] = request.system_prompt_suffix
        return SimpleNamespace(content="web answer", reasoning_signature=None)

    async def fake_finalize(**_kwargs):
        return False

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_load_runtime_context", fake_load_context)
    monkeypatch.setattr(runtime, "_maybe_handle_plan_mode_entry", noop_async)
    monkeypatch.setattr(runtime, "invoke_agent", fake_invoke)
    monkeypatch.setattr(runtime, "_finalize_web_chat_run_with_assistant", fake_finalize)
    monkeypatch.setattr(runtime, "_persist_runtime_event", noop_async)
    monkeypatch.setattr(runtime, "_persist_tool_call", noop_async)
    monkeypatch.setattr(runtime, "_update_runtime_task", noop_async)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", noop_async)
    monkeypatch.setattr(runtime, "_resume_queued_plan_handoffs", noop_async)

    await runtime.execute_web_chat_run(run_id)

    assert "send_channel_message" not in captured["excluded_tool_names"]
    assert "send_channel_file" not in captured["excluded_tool_names"]
    assert "only call them when the user explicitly asks" in captured["system_prompt_suffix"]


@pytest.mark.asyncio
async def test_execute_web_chat_run_allows_channel_delivery_tools_for_typed_web_authorization(monkeypatch):
    import app.services.web_chat_runtime as runtime
    from app.services.channel_delivery_service import channel_delivery_target

    run_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    target = {"channel": "feishu", "chat_id": "oc_x"}
    runtime_task = SimpleNamespace(
        id=run_id,
        parent_agent_id=agent_id,
        parent_session_id=session_id,
        prompt="把这份报告发送到飞书",
        metadata_json={
            "user_id": str(user_id),
            "session_id": session_id,
            "source": "web",
            "allow_channel_delivery_tools": True,
        },
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="Agent",
        role_description="",
        primary_model_id=uuid4(),
        fallback_model_id=None,
        tenant_id=uuid4(),
        agent_type="native",
    )
    user = SimpleNamespace(id=user_id, display_name="Rocky", username="rocky")
    llm_model = SimpleNamespace(provider="openai", model="gpt-4.1", supports_vision=False)
    captured = {}

    async def fake_load_context(_run_uuid):
        return runtime_task, agent, user, llm_model, None, [], SimpleNamespace(delivery_target_json=target)

    async def fake_invoke(request):
        captured["excluded_tool_names"] = request.excluded_tool_names
        captured["delivery_target"] = channel_delivery_target.get(None)
        return SimpleNamespace(content="sent", reasoning_signature=None)

    async def fake_finalize(**_kwargs):
        return False

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_load_runtime_context", fake_load_context)
    monkeypatch.setattr(runtime, "_maybe_handle_plan_mode_entry", noop_async)
    monkeypatch.setattr(runtime, "invoke_agent", fake_invoke)
    monkeypatch.setattr(runtime, "_finalize_web_chat_run_with_assistant", fake_finalize)
    monkeypatch.setattr(runtime, "_persist_runtime_event", noop_async)
    monkeypatch.setattr(runtime, "_persist_tool_call", noop_async)
    monkeypatch.setattr(runtime, "_update_runtime_task", noop_async)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", noop_async)
    monkeypatch.setattr(runtime, "_resume_queued_plan_handoffs", noop_async)

    await runtime.execute_web_chat_run(run_id)

    assert "send_channel_message" not in captured["excluded_tool_names"]
    assert "send_channel_file" not in captured["excluded_tool_names"]
    assert captured["delivery_target"] == target


def test_conversation_reload_reuses_frozen_tool_result_bytes_and_call_id():
    from app.services.web_chat_runtime import conversation_from_history_messages

    payload = {
        "name": "read_file",
        "args": {"path": "large.txt"},
        "status": "done",
        "tool_call_id": "call_original",
        "result": "R" * 60_000,
        "content_replacement": {
            "schema": "content_replacement_record.v1",
            "tool_call_id": "call_original",
            "inline_content": "MODEL-SEEN-BYTES",
            "original_chars": 60_000,
            "inline_chars": 16,
        },
    }
    history = [
        SimpleNamespace(
            role="tool_call",
            id="db-message-id",
            content=json.dumps(payload),
        )
    ]

    conversation = conversation_from_history_messages(history)

    assert conversation[0]["tool_calls"][0]["id"] == "call_original"
    assert conversation[1]["tool_call_id"] == "call_original"
    assert conversation[1]["content"] == "MODEL-SEEN-BYTES"


def test_personal_knowledge_replay_projection_keeps_references_not_content() -> None:
    from app.services.web_chat_runtime import _knowledge_tool_replay_projection

    document_id = str(uuid4())
    segment_id = str(uuid4())
    source_ref = f"kb://person/owner/documents/{document_id}#segment={segment_id}"
    projection = _knowledge_tool_replay_projection(
        tool_name="search_personal_kb",
        args={"query": "operating notes", "limit": 5},
        raw_result=json.dumps(
            {
                "results": [
                    {
                        "document_id": document_id,
                        "segment_id": segment_id,
                        "title": "PRIVATE-TITLE",
                        "snippet": "PRIVATE-SNIPPET",
                        "source_ref": source_ref,
                        "score_trace": {"secret": "PRIVATE-TRACE"},
                    }
                ],
                "warnings": [],
            }
        ),
    )

    assert projection is not None
    payload = json.loads(projection)
    assert payload == {
        "schema": "knowledge_tool_replay.v1",
        "tool_name": "search_personal_kb",
        "scope": "personal",
        "query": "operating notes",
        "result_count": 1,
        "references": [
            {
                "document_id": document_id,
                "segment_id": segment_id,
                "source_ref": source_ref,
            }
        ],
        "content_omitted": True,
        "instruction": "Call search_personal_kb/read_personal_kb again if the content is needed.",
    }
    assert "PRIVATE-TITLE" not in projection
    assert "PRIVATE-SNIPPET" not in projection
    assert "PRIVATE-TRACE" not in projection


def test_personal_knowledge_read_replay_projection_omits_segment_body() -> None:
    from app.services.web_chat_runtime import _knowledge_tool_replay_projection

    document_id = str(uuid4())
    segment_id = str(uuid4())
    source_ref = f"kb://person/owner/documents/{document_id}#segment={segment_id}"
    projection = _knowledge_tool_replay_projection(
        tool_name="read_personal_kb",
        args={"document_id": document_id, "segment_ids": [segment_id]},
        raw_result=json.dumps(
            {
                "document_id": document_id,
                "title": "PRIVATE-TITLE",
                "segments": [
                    {
                        "segment_id": segment_id,
                        "content": "PRIVATE-BODY",
                        "source_ref": source_ref,
                    }
                ],
                "warnings": [],
            }
        ),
    )

    assert projection is not None
    payload = json.loads(projection)
    assert payload["tool_name"] == "read_personal_kb"
    assert payload["references"] == [{"document_id": document_id, "segment_id": segment_id, "source_ref": source_ref}]
    assert "PRIVATE-TITLE" not in projection
    assert "PRIVATE-BODY" not in projection


def test_company_knowledge_replay_projection_keeps_publication_references_not_content() -> None:
    from app.services.web_chat_runtime import _knowledge_tool_replay_projection

    publication_id = str(uuid4())
    document_id = str(uuid4())
    segment_id = str(uuid4())
    source_ref = f"company-publication://{publication_id}/documents/{document_id}#segment={segment_id}"
    projection = _knowledge_tool_replay_projection(
        tool_name="read_company_kb",
        args={"publication_id": publication_id, "document_id": document_id},
        raw_result=json.dumps(
            {
                "publication_id": publication_id,
                "document_id": document_id,
                "title": "COMPANY-PRIVATE-TITLE",
                "segments": [
                    {
                        "document_id": document_id,
                        "segment_id": segment_id,
                        "content": "COMPANY-PRIVATE-BODY",
                        "source_ref": source_ref,
                    }
                ],
                "citations": ["company-evidence://private"],
                "warnings": [],
            }
        ),
    )

    assert projection is not None
    payload = json.loads(projection)
    assert payload == {
        "schema": "knowledge_tool_replay.v1",
        "tool_name": "read_company_kb",
        "scope": "company",
        "result_count": 1,
        "references": [
            {
                "publication_id": publication_id,
                "document_id": document_id,
                "segment_id": segment_id,
                "source_ref": source_ref,
            }
        ],
        "content_omitted": True,
        "instruction": "Call search_company_kb/read_company_kb again if the content is needed.",
    }
    assert "COMPANY-PRIVATE-TITLE" not in projection
    assert "COMPANY-PRIVATE-BODY" not in projection
    assert "company-evidence://private" not in projection


def test_conversation_reload_surfaces_malformed_tool_call_record() -> None:
    from app.services.web_chat_runtime import conversation_from_history_messages

    history = [
        SimpleNamespace(
            role="tool_call",
            id="bad-tool-row",
            content="{not valid json",
        )
    ]

    conversation = conversation_from_history_messages(history)

    assert conversation == [
        {
            "role": "system",
            "content": (
                "[Tool replay repair] A persisted tool_call record could not be reconstructed "
                "(message_id=bad-tool-row). Treat that tool result as unavailable and do not claim it succeeded."
            ),
        }
    ]


def test_terminal_artifact_paths_require_current_turn_provenance():
    import app.services.web_chat_runtime as runtime
    from app.runtime.session import SessionContext

    context = SessionContext()
    context.track_file_write("workspace/old.md")
    context.begin_turn()

    assert context.recent_writes == ["workspace/old.md"]
    assert runtime._terminal_file_change_paths_for_turn(context) == []
    assert runtime._terminal_artifact_paths_for_turn(context, "DELIVERABLE: workspace/old.md") == []

    context.track_file_write("workspace/new.md")
    context.track_file_write("workspace/scratch.md")

    assert context.recent_writes == ["workspace/old.md", "workspace/new.md", "workspace/scratch.md"]
    assert runtime._terminal_file_change_paths_for_turn(context) == ["workspace/new.md", "workspace/scratch.md"]
    assert runtime._terminal_artifact_paths_for_turn(
        context,
        "\n".join(
            [
                "完成。",
                "DELIVERABLE: workspace/new.md",
                "DELIVERABLE: workspace/old.md",
                "交付物: `workspace/missing.md`",
            ]
        ),
    ) == ["workspace/new.md"]

    assert (
        runtime._terminal_artifact_paths_for_turn(
            context,
            "完成，最终文档在 `workspace/scratch.md`。",
        )
        == []
    )


def test_terminal_artifact_paths_accept_final_summary_mentions_and_single_doc_fallback():
    import app.services.web_chat_runtime as runtime
    from app.runtime.session import SessionContext

    context = SessionContext()
    context.begin_turn()
    context.track_file_write("workspace/report.md")
    context.track_file_write(".ultra/debug/subagent-log.jsonl")
    context.track_file_write("workspace/scratch.md")

    assert runtime._terminal_artifact_paths_for_turn(
        context,
        "已完成，最终文档见 `workspace/report.md`；旧稿在 workspace/old.md。",
    ) == ["workspace/report.md"]
    assert runtime._rejected_terminal_artifact_paths_for_turn(
        context,
        "已完成，最终文档见 `workspace/report.md`；旧稿在 workspace/old.md。",
    ) == ["workspace/old.md"]

    single_doc_context = SessionContext()
    single_doc_context.begin_turn()
    single_doc_context.track_file_write("workspace/final-report.md")
    single_doc_context.track_file_write(".ultra/compact-snapshot.md")
    single_doc_context.track_file_write("workspace/scratch-notes.md")

    assert runtime._terminal_artifact_paths_for_turn(single_doc_context, "已完成。") == ["workspace/final-report.md"]


@pytest.mark.asyncio
async def test_execute_web_chat_run_resets_turn_writes_and_scopes_deliverables(monkeypatch):
    import app.services.web_chat_runtime as runtime
    from app.runtime.session import SessionContext

    run_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4().hex
    runtime_task = SimpleNamespace(
        id=run_id,
        parent_agent_id=agent_id,
        parent_session_id=session_id,
        prompt="write the new report",
        metadata_json={"user_id": str(user_id), "session_id": session_id, "source": "web", "turn_id": "turn-current"},
        trace_id=f"web_chat_turn:{run_id.hex}",
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="Agent",
        role_description="",
        primary_model_id=uuid4(),
        fallback_model_id=None,
        tenant_id=tenant_id,
        agent_type="native",
    )
    user = SimpleNamespace(id=user_id, display_name="Rocky", username="rocky")
    llm_model = SimpleNamespace(provider="openai", model="gpt-4.1", supports_vision=False)
    runtime_context = SessionContext(session_id=session_id, source="web", channel="web")
    runtime_context.track_file_write("workspace/old.md")
    captured: dict[str, object] = {}

    async def fake_load_context(_run_uuid):
        return (runtime_task, agent, user, llm_model, None, [], SimpleNamespace(delivery_target_json=None))

    async def fake_runtime_session(_agent_id, _session_id):
        return runtime_context

    async def fake_invoke(request):
        assert request.session_context is runtime_context
        assert runtime_context.current_turn_writes == []
        assert runtime_context.recent_writes == ["workspace/old.md"]
        assert runtime_context.metadata["runtime_task_id"] == run_id.hex
        assert runtime_context.metadata["turn_id"] == "turn-current"
        runtime_context.track_file_write(
            "workspace/new.md",
            snapshot={
                "path": "workspace/new.md",
                "exists": True,
                "sha256": "f" * 64,
                "size": 3,
            },
            lineage={
                "path": "workspace/new.md",
                "before_state": {
                    "path": "workspace/new.md",
                    "exists": False,
                    "sha256": None,
                    "size": 0,
                },
                "after_state": {
                    "path": "workspace/new.md",
                    "exists": True,
                    "sha256": "f" * 64,
                    "size": 3,
                },
            },
        )
        return SimpleNamespace(
            content="\n".join(
                [
                    "完成。",
                    "DELIVERABLE: workspace/new.md",
                    "DELIVERABLE: workspace/old.md",
                ]
            ),
            reasoning_signature=None,
        )

    async def fake_finalize(**kwargs):
        captured.update(kwargs)
        return True

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_load_runtime_context", fake_load_context)
    monkeypatch.setattr(runtime.web_chat_broker, "get_or_create_runtime_session", fake_runtime_session)
    monkeypatch.setattr(runtime, "_maybe_handle_plan_mode_entry", noop_async)
    monkeypatch.setattr(runtime, "invoke_agent", fake_invoke)
    monkeypatch.setattr(runtime, "_finalize_web_chat_run_with_assistant", fake_finalize)
    monkeypatch.setattr(runtime, "_persist_runtime_event", noop_async)
    monkeypatch.setattr(runtime, "_persist_tool_call", noop_async)
    monkeypatch.setattr(runtime, "_update_runtime_task", noop_async)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", noop_async)
    monkeypatch.setattr(runtime, "_emit_terminal_turn_hook", noop_async)

    await runtime.execute_web_chat_run(run_id)

    assert captured["artifact_paths"] == ["workspace/new.md"]
    assert captured["file_change_paths"] == ["workspace/new.md"]
    assert captured["file_change_states"]["workspace/new.md"]["sha256"] == "f" * 64
    assert captured["file_change_lineage"][0]["path"] == "workspace/new.md"
    assert captured["declared_artifact_paths"] == ["workspace/new.md", "workspace/old.md"]
    assert captured["rejected_artifact_paths"] == ["workspace/old.md"]
    assert runtime_context.current_turn_writes == ["workspace/new.md"]


@pytest.mark.asyncio
async def test_execute_web_chat_run_records_turn_tokens_for_goal_accounting(monkeypatch):
    """A4: the terminal metadata carries this invocation's token total so the goal
    continuation bridge can charge it against the session goal's budget."""
    import app.services.web_chat_runtime as runtime
    from app.runtime.session import SessionContext

    run_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    runtime_task = SimpleNamespace(
        id=run_id,
        parent_agent_id=agent_id,
        parent_session_id=session_id,
        prompt="do the work",
        metadata_json={"user_id": str(user_id), "session_id": session_id, "source": "web"},
        trace_id=f"web_chat_turn:{run_id.hex}",
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="Agent",
        role_description="",
        primary_model_id=uuid4(),
        fallback_model_id=None,
        tenant_id=uuid4(),
        agent_type="native",
    )
    user = SimpleNamespace(id=user_id, display_name="Rocky", username="rocky")
    llm_model = SimpleNamespace(provider="openai", model="gpt-4.1", supports_vision=False)
    runtime_context = SessionContext(session_id=session_id, source="web", channel="web")
    captured: dict[str, object] = {}

    async def fake_load_context(_run_uuid):
        return (runtime_task, agent, user, llm_model, None, [], SimpleNamespace(delivery_target_json=None))

    async def fake_runtime_session(_agent_id, _session_id):
        return runtime_context

    async def fake_invoke(_request):
        return SimpleNamespace(content="done.", reasoning_signature=None, tokens_used=4242)

    async def fake_finalize(**kwargs):
        captured.update(kwargs)
        return True

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_load_runtime_context", fake_load_context)
    monkeypatch.setattr(runtime.web_chat_broker, "get_or_create_runtime_session", fake_runtime_session)
    monkeypatch.setattr(runtime, "_maybe_handle_plan_mode_entry", noop_async)
    monkeypatch.setattr(runtime, "invoke_agent", fake_invoke)
    monkeypatch.setattr(runtime, "_finalize_web_chat_run_with_assistant", fake_finalize)
    monkeypatch.setattr(runtime, "_persist_runtime_event", noop_async)
    monkeypatch.setattr(runtime, "_persist_tool_call", noop_async)
    monkeypatch.setattr(runtime, "_update_runtime_task", noop_async)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", noop_async)
    monkeypatch.setattr(runtime, "_emit_terminal_turn_hook", noop_async)

    await runtime.execute_web_chat_run(run_id)

    metadata_json = captured["metadata_json"]
    assert isinstance(metadata_json, dict)
    assert metadata_json["turn_tokens_used"] == 4242


def _phase_run_fixtures():
    run_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    runtime_task = SimpleNamespace(
        id=run_id,
        parent_agent_id=agent_id,
        parent_session_id=session_id,
        prompt="do the work",
        metadata_json={"user_id": str(user_id), "session_id": session_id, "source": "web"},
        trace_id=f"web_chat_turn:{run_id.hex}",
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="Agent",
        role_description="",
        primary_model_id=uuid4(),
        fallback_model_id=None,
        tenant_id=uuid4(),
        agent_type="native",
    )
    user = SimpleNamespace(id=user_id, display_name="Rocky", username="rocky")
    llm_model = SimpleNamespace(provider="openai", model="gpt-4.1", supports_vision=False)
    return run_id, agent, user, llm_model, runtime_task


def _patch_phase_run(
    monkeypatch,
    runtime,
    *,
    runtime_task,
    agent,
    user,
    llm_model,
    fake_invoke,
    events,
    persist_stream_steps: bool = False,
):
    import app.services.web_chat_run_orchestrator as orchestrator
    from app.runtime.session import SessionContext

    runtime_context = SessionContext(session_id=str(runtime_task.parent_session_id), source="web", channel="web")

    async def fake_load_context(_run_uuid):
        return (runtime_task, agent, user, llm_model, None, [], SimpleNamespace(delivery_target_json=None))

    async def fake_runtime_session(_agent_id, _session_id):
        return runtime_context

    async def capture_broadcast(_agent_id, _session_id, event):
        events.append(event)

    async def fake_finalize(**_kwargs):
        return True

    async def fake_prepare_model_request(state, **_payload):
        provider_request_id = f"test:{state.run_uuid}:round:1:attempt:1"
        state.active_provider_request_id = provider_request_id
        return provider_request_id

    async def invoke_with_model_request_fence(request):
        await request.model_request_prepare(
            round_index=1,
            messages=[],
            tools=[],
            provider="openai",
            model="gpt-4.1",
            wire_request={},
        )
        return await fake_invoke(request)

    async def fake_persist_stream_step(**kwargs):
        return {
            "type": kwargs["event_type"],
            "content": kwargs["content"],
            "visibility": {"audience": "direct_user"},
        }

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_load_runtime_context", fake_load_context)
    monkeypatch.setattr(runtime.web_chat_broker, "get_or_create_runtime_session", fake_runtime_session)
    monkeypatch.setattr(runtime, "_maybe_handle_plan_mode_entry", noop_async)
    monkeypatch.setattr(runtime, "invoke_agent", invoke_with_model_request_fence)
    monkeypatch.setattr(orchestrator, "_prepare_session_model_request", fake_prepare_model_request)
    monkeypatch.setattr(runtime, "_finalize_web_chat_run_with_assistant", fake_finalize)
    monkeypatch.setattr(runtime, "_persist_runtime_event", noop_async)
    monkeypatch.setattr(runtime, "_persist_tool_call", noop_async)
    monkeypatch.setattr(runtime, "_update_runtime_task", noop_async)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", capture_broadcast)
    monkeypatch.setattr(runtime, "_emit_terminal_turn_hook", noop_async)
    monkeypatch.setattr(runtime, "_resume_queued_plan_handoffs", noop_async)
    if not persist_stream_steps:
        monkeypatch.setattr(runtime, "_persist_stream_step_event", fake_persist_stream_step)


@pytest.mark.asyncio
async def test_execute_web_chat_run_emits_first_class_phase_signal(monkeypatch):
    """§3 seam 1: one clean RuntimePhase stream — starting → thinking →
    tool_running → thinking → responding → done — emitted as `phase` events."""
    import app.services.web_chat_runtime as runtime

    run_id, agent, user, llm_model, runtime_task = _phase_run_fixtures()
    events: list[dict] = []

    async def fake_invoke(request):
        await request.on_thinking("pondering")
        await request.on_tool_call({"name": "write_file", "status": "running", "args": {}})
        await request.on_tool_call({"name": "write_file", "status": "done", "result": "{}"})
        await request.on_chunk("hello")
        return SimpleNamespace(content="done.", reasoning_signature=None, tokens_used=1)

    _patch_phase_run(
        monkeypatch,
        runtime,
        runtime_task=runtime_task,
        agent=agent,
        user=user,
        llm_model=llm_model,
        fake_invoke=fake_invoke,
        events=events,
    )

    await runtime.execute_web_chat_run(run_id)

    phase_events = [event for event in events if event.get("type") == "phase"]
    assert [event["phase"] for event in phase_events] == [
        "starting",
        "thinking",
        "tool_running",
        "thinking",
        "responding",
        "done",
    ]
    tool_phase = next(event for event in phase_events if event["phase"] == "tool_running")
    assert tool_phase["detail"] == {"tool_name": "write_file"}
    assert all(event["run_id"] == run_id.hex for event in phase_events)


@pytest.mark.asyncio
async def test_execute_web_chat_run_exposes_reclaimed_claim_as_resuming(monkeypatch):
    import app.services.web_chat_runtime as runtime

    run_id, agent, user, llm_model, runtime_task = _phase_run_fixtures()
    runtime_task.metadata_json["reclaimed_expired_claim"] = True
    events: list[dict] = []

    async def fake_invoke(_request):
        return SimpleNamespace(content="recovered", reasoning_signature=None, tokens_used=1)

    _patch_phase_run(
        monkeypatch,
        runtime,
        runtime_task=runtime_task,
        agent=agent,
        user=user,
        llm_model=llm_model,
        fake_invoke=fake_invoke,
        events=events,
    )

    await runtime.execute_web_chat_run(run_id)

    phase_events = [event for event in events if event.get("type") == "phase"]
    assert phase_events[0]["phase"] == "resuming"


@pytest.mark.asyncio
async def test_execute_web_chat_run_persists_stream_steps_for_replay(monkeypatch):
    import app.services.web_chat_runtime as runtime
    import app.services.session_model_round as session_model_round

    run_id, agent, user, llm_model, runtime_task = _phase_run_fixtures()
    events: list[dict] = []
    persisted: list[dict] = []

    async def fake_invoke(request):
        await request.on_chunk("I will inspect the session renderer.")
        await request.on_thinking("Checking the transcript contract.")
        await request.on_tool_call(
            {
                "tool_call_id": "tool-read",
                "name": "read_file",
                "status": "running",
                "args": {"path": "frontend/src/pages/agent-detail/chatRuntime.ts"},
            }
        )
        await request.on_tool_call(
            {
                "tool_call_id": "tool-read",
                "name": "read_file",
                "status": "done",
                "args": {"path": "frontend/src/pages/agent-detail/chatRuntime.ts"},
                "result": "read ok",
            }
        )
        return SimpleNamespace(content="done.", reasoning_signature=None, tokens_used=1)

    _patch_phase_run(
        monkeypatch,
        runtime,
        runtime_task=runtime_task,
        agent=agent,
        user=user,
        llm_model=llm_model,
        fake_invoke=fake_invoke,
        events=events,
        persist_stream_steps=True,
    )

    class _FakeTenantSession:
        async def __aenter__(self):
            return _StreamDB()

        async def __aexit__(self, *_args):
            return False

    class _StreamDB(_FakeDB):
        async def scalar(self, _statement):
            return SimpleNamespace(
                envelope_json={
                    "type": "session.event",
                    "visibility": {"audience": "direct_user"},
                }
            )

    async def fake_append_model_stream_delta(_db, **kwargs):
        persisted.append(kwargs)
        return SimpleNamespace(id=uuid4())

    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda _tenant_id: _FakeTenantSession())
    monkeypatch.setattr(session_model_round, "append_model_stream_delta", fake_append_model_stream_delta)

    await runtime.execute_web_chat_run(run_id)

    assert [(event["phase"], event["content"]) for event in persisted] == [
        ("unknown", "I will inspect the session renderer."),
        ("reasoning_private", "Checking the transcript contract."),
    ]
    assert all(event["run_id"] == run_id for event in persisted)
    assert all(event["lifecycle"] == "delta" for event in persisted)
    assert all(event["provider_request_id"].startswith("test:") for event in persisted)


@pytest.mark.asyncio
async def test_execute_web_chat_run_emits_failed_phase_on_exception(monkeypatch):
    """The finally backstop settles the phase stream even when the run explodes."""
    import app.services.web_chat_runtime as runtime

    run_id, agent, user, llm_model, runtime_task = _phase_run_fixtures()
    events: list[dict] = []

    async def fake_invoke(_request):
        raise RuntimeError("provider exploded")

    _patch_phase_run(
        monkeypatch,
        runtime,
        runtime_task=runtime_task,
        agent=agent,
        user=user,
        llm_model=llm_model,
        fake_invoke=fake_invoke,
        events=events,
    )

    await runtime.execute_web_chat_run(run_id)

    phase_events = [event for event in events if event.get("type") == "phase"]
    assert [event["phase"] for event in phase_events] == ["starting", "failed"]


@pytest.mark.asyncio
async def test_execute_web_chat_run_summary_turn_holds_summarizing_phase(monkeypatch):
    """§2: the budget finalization turn holds `summarizing` for its whole
    duration — thinking/chunk/tool events must not cycle the phase."""
    import app.services.web_chat_runtime as runtime

    run_id, agent, user, llm_model, runtime_task = _phase_run_fixtures()
    runtime_task.metadata_json["budget_summary_turn"] = True
    events: list[dict] = []

    async def fake_invoke(request):
        assert request.session_context.metadata.get("budget_summary_turn") is True
        await request.on_thinking("wrapping up")
        await request.on_chunk("Final report: ...")
        return SimpleNamespace(content="Final report.", reasoning_signature=None, tokens_used=1)

    _patch_phase_run(
        monkeypatch,
        runtime,
        runtime_task=runtime_task,
        agent=agent,
        user=user,
        llm_model=llm_model,
        fake_invoke=fake_invoke,
        events=events,
    )

    await runtime.execute_web_chat_run(run_id)

    phase_events = [event for event in events if event.get("type") == "phase"]
    assert [event["phase"] for event in phase_events] == ["summarizing", "done"]


@pytest.mark.asyncio
async def test_execute_web_chat_run_awaiting_approval_phase_survives_run_release(monkeypatch):
    """A session-permission pause parks the phase at awaiting_approval, not done."""
    import app.services.web_chat_runtime as runtime

    run_id, agent, user, llm_model, runtime_task = _phase_run_fixtures()
    events: list[dict] = []

    async def fake_invoke(request):
        await request.on_tool_call(
            {
                "name": "send_email",
                "status": "done",
                "result": json.dumps({"status": "session_permission_required", "message": "approve?"}),
            }
        )
        return SimpleNamespace(content="", reasoning_signature=None, tokens_used=1)

    _patch_phase_run(
        monkeypatch,
        runtime,
        runtime_task=runtime_task,
        agent=agent,
        user=user,
        llm_model=llm_model,
        fake_invoke=fake_invoke,
        events=events,
    )

    async def fake_finalize_without_assistant(**_kwargs):
        return True

    monkeypatch.setattr(runtime, "_finalize_web_chat_run_without_assistant", fake_finalize_without_assistant)

    await runtime.execute_web_chat_run(run_id)

    phase_events = [event for event in events if event.get("type") == "phase"]
    assert phase_events[-1]["phase"] == "awaiting_approval"
    assert "done" not in {event["phase"] for event in phase_events}


def test_web_chat_final_message_has_database_idempotency_guard():
    from pathlib import Path

    migration = Path("alembic/versions/web_chat_final_message_idempotency_0702.py")
    assert migration.exists()
    text = migration.read_text(encoding="utf-8")
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_messages_web_chat_final_decision_trace" in text
    assert "WHERE decision_trace_id LIKE 'web_chat_final:%'" in text


def test_final_assistant_marker_unique_violation_detects_idempotency_index():
    import app.services.web_chat_runtime as runtime
    from sqlalchemy.exc import IntegrityError

    class _Orig:
        diag = SimpleNamespace(constraint_name="uq_chat_messages_web_chat_final_decision_trace")

    exc = IntegrityError("insert chat_messages", {}, _Orig())

    assert runtime._is_final_assistant_marker_unique_violation(exc)


class _QueuedScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        if isinstance(self._value, list):
            return self._value[0] if self._value else None
        return self._value

    def scalars(self):
        values = self._value if isinstance(self._value, list) else ([] if self._value is None else [self._value])
        return SimpleNamespace(all=lambda: values)


class _QueuedDB:
    def __init__(self, *values):
        self.values = list(values)

    async def execute(self, _stmt):
        return _QueuedScalarResult(self.values.pop(0) if self.values else None)


def _history_msg(
    *,
    msg_id=None,
    role: str = "user",
    content: str = "hello",
    created_at: datetime | None = None,
):
    return SimpleNamespace(
        id=msg_id or uuid4(),
        role=role,
        content=content,
        created_at=created_at or datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_load_runtime_context_resolves_model_inside_tenant_transaction(monkeypatch):
    import app.services.model_resolution as model_resolution
    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    model_id = uuid4()

    task = SimpleNamespace(
        id=run_id,
        parent_agent_id=agent_id,
        parent_session_id=str(session_id),
        status="pending",
        started_at=None,
        metadata_json={"user_id": str(user_id)},
    )
    session_row = SimpleNamespace(id=session_id, transcript_metadata_json={})
    agent = SimpleNamespace(
        id=agent_id,
        name="Agent",
        tenant_id=tenant_id,
        primary_model_id=model_id,
        fallback_model_id=None,
        sponsor=None,
        deleted_at=None,
        deactivated_at=None,
    )
    user = SimpleNamespace(id=user_id)
    model = SimpleNamespace(id=model_id, provider="minimax", model="minimax-m3", max_input_tokens=200000)

    class _Session:
        def __init__(self):
            self.commits = 0
            self.calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _stmt):
            self.calls += 1
            if self.calls == 1:
                return _QueuedScalarResult(task)
            if self.calls == 2:
                return _QueuedScalarResult(session_row)
            if self.calls == 3:
                return _QueuedScalarResult(agent)
            if self.calls == 4:
                return _QueuedScalarResult(user)
            if self.calls == 5:
                return _QueuedScalarResult(model)
            return _QueuedScalarResult([])

        async def commit(self):
            self.commits += 1

    class _TenantContext:
        def __init__(self, db):
            self.db = db

        async def __aenter__(self):
            return self.db

        async def __aexit__(self, *_args):
            await self.db.commit()
            return False

    db = _Session()

    async def noop_materialize(**_kwargs):
        return None

    async def noop_projection(_db, _session, messages):
        return messages

    async def no_default_model(*_args, **_kwargs):
        return None

    async def resolve_run_tenant(_run_id, **_kwargs):
        return tenant_id

    monkeypatch.setattr(runtime, "resolve_tenant_for_runtime_task", resolve_run_tenant)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda *a, **k: _TenantContext(db))
    monkeypatch.setattr(runtime, "_materialize_initial_user_turn_for_worker", noop_materialize)
    monkeypatch.setattr(runtime, "_apply_active_projection_to_history", noop_projection)
    monkeypatch.setattr(model_resolution, "resolve_default_model_for_tenant", no_default_model)

    _task, _agent, _user, resolved_model, _fallback, _history, _session = await runtime._load_runtime_context(run_id)

    assert resolved_model is model
    assert db.commits == 1


@pytest.mark.asyncio
async def test_load_runtime_context_uses_validated_team_member_model_override(monkeypatch):
    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    primary_model_id = uuid4()
    team_model_id = uuid4()
    task = SimpleNamespace(
        id=run_id,
        tenant_id=tenant_id,
        parent_agent_id=agent_id,
        parent_session_id=str(session_id),
        status="pending",
        started_at=None,
        budget_run_id=None,
        budget_admission_status=None,
        budget_snapshot_json=None,
        metadata_json={
            "user_id": str(user_id),
            "runtime_model_id": str(team_model_id),
            "runtime_model_source": "agent_team_member",
        },
    )
    session_row = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        tenant_id=tenant_id,
        transcript_metadata_json={},
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="Agent",
        tenant_id=tenant_id,
        primary_model_id=primary_model_id,
        fallback_model_id=None,
        sponsor=None,
        deleted_at=None,
        deactivated_at=None,
    )
    user = SimpleNamespace(id=user_id, tenant_id=tenant_id)
    team_model = SimpleNamespace(
        id=team_model_id,
        tenant_id=tenant_id,
        enabled=True,
        provider="openai",
        model="team-review-model",
        max_input_tokens=200000,
    )
    captured = {}

    class _Session:
        def __init__(self):
            self.calls = 0

        async def execute(self, _stmt):
            self.calls += 1
            values = [task, session_row, agent, user, []]
            return _QueuedScalarResult(values[self.calls - 1] if self.calls <= len(values) else [])

        async def commit(self):
            return None

    class _TenantContext:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_args):
            await db.commit()
            return False

    async def resolve_models(_db, *, agent, metadata):
        captured["agent"] = agent
        captured["metadata"] = dict(metadata)
        return team_model, None

    async def noop_materialize(**_kwargs):
        return None

    async def noop_projection(_db, _session, messages):
        return messages

    async def resolve_run_tenant(_run_id, **_kwargs):
        return tenant_id

    db = _Session()
    monkeypatch.setattr(runtime, "resolve_tenant_for_runtime_task", resolve_run_tenant)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda *a, **k: _TenantContext())
    monkeypatch.setattr(runtime, "_materialize_initial_user_turn_for_worker", noop_materialize)
    monkeypatch.setattr(runtime, "_apply_active_projection_to_history", noop_projection)
    monkeypatch.setattr(runtime, "_resolve_runtime_models_for_task", resolve_models)

    _task, _agent, _user, resolved_model, fallback, _history, _session = await runtime._load_runtime_context(run_id)

    assert resolved_model is team_model
    assert fallback is None
    assert captured["agent"] is agent
    assert captured["metadata"]["runtime_model_id"] == str(team_model_id)


@pytest.mark.asyncio
async def test_runtime_model_resolver_honors_exact_enabled_team_member_model(monkeypatch):
    import app.services.model_resolution as model_resolution
    import app.services.web_chat_runtime as runtime

    tenant_id = uuid4()
    primary_model_id = uuid4()
    team_model_id = uuid4()
    agent = SimpleNamespace(
        tenant_id=tenant_id,
        primary_model_id=primary_model_id,
        fallback_model_id=None,
    )
    team_model = SimpleNamespace(id=team_model_id, tenant_id=tenant_id, enabled=True)

    class _ModelDB:
        async def execute(self, _statement):
            return _ScalarResult(team_model)

    async def no_default_model(*_args, **_kwargs):
        return None

    monkeypatch.setattr(model_resolution, "resolve_default_model_for_tenant", no_default_model)

    primary, fallback = await runtime._resolve_runtime_models_for_task(
        _ModelDB(),
        agent=agent,
        metadata={"runtime_model_id": str(team_model_id)},
    )

    assert primary is team_model
    assert fallback is None


@pytest.mark.asyncio
async def test_runtime_model_resolver_rejects_invalid_or_unavailable_override() -> None:
    import app.services.web_chat_runtime as runtime

    tenant_id = uuid4()
    agent = SimpleNamespace(
        tenant_id=tenant_id,
        primary_model_id=uuid4(),
        fallback_model_id=None,
    )

    class _MissingModelDB:
        async def execute(self, _statement):
            return _ScalarResult(None)

    with pytest.raises(RuntimeError, match="invalid runtime_model_id"):
        await runtime._resolve_runtime_models_for_task(
            _MissingModelDB(),
            agent=agent,
            metadata={"runtime_model_id": "not-a-uuid"},
        )

    with pytest.raises(RuntimeError, match="unavailable in the Agent tenant"):
        await runtime._resolve_runtime_models_for_task(
            _MissingModelDB(),
            agent=agent,
            metadata={"runtime_model_id": str(uuid4())},
        )


@pytest.mark.asyncio
async def test_load_runtime_context_rejects_runtime_task_agent_tenant_mismatch(monkeypatch):
    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    session_id = uuid4()
    user_id = uuid4()
    agent_tenant_id = uuid4()
    task_tenant_id = uuid4()
    task = SimpleNamespace(
        id=run_id,
        tenant_id=task_tenant_id,
        parent_agent_id=agent_id,
        parent_session_id=str(session_id),
        status="running",
        started_at=datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc),
        metadata_json={"user_id": str(user_id)},
    )
    session_row = SimpleNamespace(
        id=session_id, agent_id=agent_id, tenant_id=agent_tenant_id, transcript_metadata_json={}
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="Agent",
        tenant_id=agent_tenant_id,
        primary_model_id=None,
        fallback_model_id=None,
        sponsor=None,
        deleted_at=None,
        deactivated_at=None,
    )
    user = SimpleNamespace(id=user_id, tenant_id=agent_tenant_id)

    class _Session:
        def __init__(self):
            self.values = [task, session_row, agent, user, []]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _stmt):
            return _QueuedScalarResult(self.values.pop(0) if self.values else [])

        async def commit(self):
            return None

    class _TenantContext:
        def __init__(self, db):
            self.db = db

        async def __aenter__(self):
            return self.db

        async def __aexit__(self, *_args):
            return False

    async def noop_materialize(**_kwargs):
        return None

    async def noop_projection(_db, _session, messages):
        return messages

    db = _Session()

    async def resolve_run_tenant(_run_id, **_kwargs):
        return task_tenant_id

    monkeypatch.setattr(runtime, "resolve_tenant_for_runtime_task", resolve_run_tenant)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda *a, **k: _TenantContext(db))
    monkeypatch.setattr(runtime, "_materialize_initial_user_turn_for_worker", noop_materialize)
    monkeypatch.setattr(runtime, "_apply_active_projection_to_history", noop_projection)

    with pytest.raises(RuntimeError, match="tenant boundary mismatch"):
        await runtime._load_runtime_context(run_id)


@pytest.mark.asyncio
async def test_active_compact_projection_replaces_prior_history_and_keeps_later_tail():
    import app.services.web_chat_runtime as runtime

    applied_at = datetime(2026, 6, 27, 12, 5, tzinfo=timezone.utc)
    session = SimpleNamespace(
        id=uuid4(),
        transcript_metadata_json={
            "active_projection": {
                "projection_reason": "compact",
                "applied_at": applied_at.isoformat(),
                "replacement_messages": [
                    {"role": "system", "content": "<session_summary>compressed context</session_summary>"},
                ],
            }
        },
    )
    history = [
        _history_msg(role="user", content="old user", created_at=datetime(2026, 6, 27, 12, 1, tzinfo=timezone.utc)),
        _history_msg(
            role="assistant",
            content="old assistant",
            created_at=datetime(2026, 6, 27, 12, 2, tzinfo=timezone.utc),
        ),
        _history_msg(
            role="user", content="after compact", created_at=datetime(2026, 6, 27, 12, 6, tzinfo=timezone.utc)
        ),
    ]

    projected = await runtime._apply_active_projection_to_history(_QueuedDB(), session, history)
    conversation = runtime.conversation_from_history_messages(projected)

    assert conversation == [
        {"role": "system", "content": "<session_summary>compressed context</session_summary>"},
        {"role": "user", "content": "after compact"},
    ]


@pytest.mark.asyncio
async def test_active_rewind_projection_excludes_selected_user_checkpoint_and_keeps_later_tail():
    import app.services.web_chat_runtime as runtime

    session_id = uuid4()
    applied_at = datetime(2026, 6, 27, 12, 5, tzinfo=timezone.utc)
    checkpoint_event_id = uuid4()
    kept_message_id = uuid4()
    checkpoint_message_id = uuid4()
    dropped_answer_message_id = uuid4()
    tail_message_id = uuid4()
    session = SimpleNamespace(
        id=session_id,
        transcript_metadata_json={
            "active_projection": {
                "projection_reason": "rewind",
                "checkpoint_event_id": str(checkpoint_event_id),
                "applied_at": applied_at.isoformat(),
            }
        },
    )
    anchor_event = SimpleNamespace(id=checkpoint_event_id, session_id=session_id, sequence=10)

    class _ProjectionBoundaryDB:
        def __init__(self):
            self.calls = 0

        async def execute(self, stmt):
            self.calls += 1
            if self.calls == 1:
                return _QueuedScalarResult(anchor_event)
            sql = str(stmt)
            selected_ids = [kept_message_id, checkpoint_message_id] if "<=" in sql else [kept_message_id]
            return _QueuedScalarResult(selected_ids)

    history = [
        _history_msg(
            msg_id=kept_message_id,
            role="user",
            content="prior prompt",
            created_at=datetime(2026, 6, 27, 12, 1, tzinfo=timezone.utc),
        ),
        _history_msg(
            msg_id=checkpoint_message_id,
            role="user",
            content="selected prompt should return to composer",
            created_at=datetime(2026, 6, 27, 12, 2, tzinfo=timezone.utc),
        ),
        _history_msg(
            msg_id=dropped_answer_message_id,
            role="assistant",
            content="discarded answer",
            created_at=datetime(2026, 6, 27, 12, 3, tzinfo=timezone.utc),
        ),
        _history_msg(
            msg_id=tail_message_id,
            role="user",
            content="new prompt after rewind",
            created_at=datetime(2026, 6, 27, 12, 6, tzinfo=timezone.utc),
        ),
    ]

    projected = await runtime._apply_active_projection_to_history(
        _ProjectionBoundaryDB(),
        session,
        history,
    )
    conversation = runtime.conversation_from_history_messages(projected)

    assert conversation == [
        {"role": "user", "content": "prior prompt"},
        {"role": "user", "content": "new prompt after rewind"},
    ]


@pytest.mark.asyncio
async def test_completed_user_turn_bridges_to_goal_continuation(monkeypatch):
    import app.services.goal_continuation_service as goal_service
    import app.services.web_chat_runtime as runtime

    agent_id = uuid4()
    session_id = uuid4()
    user_id = uuid4()
    calls: list[dict] = []

    async def fake_maybe_continue_session_goal_after_turn(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "goal_id": "goal-1"}

    monkeypatch.setattr(
        goal_service,
        "maybe_continue_session_goal_after_turn",
        fake_maybe_continue_session_goal_after_turn,
    )

    result = await runtime._maybe_continue_goal_after_terminal_turn(
        db=object(),
        task=SimpleNamespace(task_type="web_chat_turn", metadata_json={"ephemeral": False}),
        agent_id=agent_id,
        session_id=session_id,
        user_id=user_id,
        status="completed",
    )

    assert result == {"ok": True, "goal_id": "goal-1"}
    assert calls[0]["agent_id"] == agent_id
    assert calls[0]["session_id"] == session_id
    assert calls[0]["user_id"] == user_id
    assert calls[0]["completed_task_type"] == "web_chat_turn"
    assert calls[0]["completed_status"] == "completed"
    assert calls[0]["metadata_json"] == {"ephemeral": False}


def test_clear_stale_plan_mode_for_plain_new_turn():
    import app.services.web_chat_runtime as runtime
    from app.runtime.session import PlanModeState, SessionContext

    context = SessionContext(session_id="session-1", source="web_chat", channel="web")
    context.plan_mode = PlanModeState(
        active=True,
        original_request="draft a plan",
        intent_type="in_session_execution",
        action_kind="start_long_task",
        tool_name="continue_current_session",
    )
    context.metadata["plan_mode"] = context.plan_mode.to_metadata()

    runtime._clear_stale_plan_mode_for_new_turn(
        context,
        plan_mode_requested=False,
        history_messages=[],
    )

    assert context.plan_mode.active is False
    assert "plan_mode" not in context.metadata


def test_preserve_plan_mode_for_blocking_clarification_reply():
    import app.services.web_chat_runtime as runtime
    from app.runtime.session import PlanModeState, SessionContext

    context = SessionContext(session_id="session-1", source="web_chat", channel="web")
    context.plan_mode = PlanModeState(
        active=True,
        original_request="draft a plan",
        intent_type="in_session_execution",
        action_kind="start_long_task",
        tool_name="continue_current_session",
    )
    context.metadata["plan_mode"] = context.plan_mode.to_metadata()
    history_messages = [
        SimpleNamespace(
            role="assistant",
            content='{"status":"awaiting_user_clarification","blocking":true,"question":"范围是什么？"}',
        )
    ]

    runtime._clear_stale_plan_mode_for_new_turn(
        context,
        plan_mode_requested=False,
        history_messages=history_messages,
    )

    assert context.plan_mode.active is True
    assert context.metadata["plan_mode"]["active"] is True


def test_interactive_pause_summary_accepts_structured_tool_payloads():
    import app.services.web_chat_runtime as runtime

    assert (
        runtime._interactive_pause_summary_for_tool_call(
            {
                "name": "ask_user_question",
                "status": "done",
                "result": {
                    "status": "awaiting_user_clarification",
                    "blocking": True,
                    "questions": [{"question": "Scope?", "options": [{"label": "A"}]}],
                },
            }
        )
        == "awaiting_user_clarification"
    )
    assert (
        runtime._interactive_pause_summary_for_tool_call(
            {
                "name": "create_digital_employee",
                "status": "done",
                "result": {
                    "status": "success",
                    "agent_id": "7a5b31cb-89b4-4053-a48e-6dfb42a8af20",
                    "message": "Successfully created digital employee.",
                },
            }
        )
        == "create_digital_employee_success"
    )
    assert (
        runtime._interactive_pause_summary_for_tool_call(
            {
                "name": "exit_plan_mode",
                "status": "done",
                "result": {
                    "status": "planning_failed",
                    "plan_id": "09df5e6f-5f3a-42a2-8b53-03b762756d20",
                    "planning_errors": ["user-visible plan leaks internal workflow detail"],
                },
            }
        )
        == "plan_mode_planning_failed"
    )


@pytest.mark.asyncio
async def test_persist_runtime_event_writes_session_native_part(monkeypatch):
    import app.services.web_chat_runtime as runtime
    import app.services.tenant_resolver as tenant_resolver

    captured: dict = {}
    tenant_id = uuid4()

    class _SessionCtx:
        async def __aenter__(self):
            return _FakeDB()

        async def __aexit__(self, *_exc):
            return False

    async def fake_resolve_tenant_for_agent(_agent_id):
        return tenant_id

    async def fake_append_session_event(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(event_id=uuid4(), sequence=1, message_id=None)

    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda _tenant_id: _SessionCtx())
    monkeypatch.setattr(runtime, "append_session_event", fake_append_session_event)

    await runtime._persist_runtime_event(
        agent_id=uuid4(),
        user_id=uuid4(),
        session_id=str(uuid4()),
        data={
            "type": "hook_progress",
            "message": "Running PreToolUse hook",
            "status": "running",
            "runtime_task_id": str(uuid4()),
            "hook_event": "PreToolUse",
            "hook_key": "guard",
        },
    )

    assert captured["event_type"] == "hook_progress"
    assert captured["role"] == "system"
    assert captured["parts"] == [
        {
            "type": "event",
            "event_type": "hook_progress",
            "title": "Hook Progress",
            "text": "Running PreToolUse hook",
            "status": "running",
            "hook_event": "PreToolUse",
            "hook_key": "guard",
            "runtime_task_id": captured["run_id"],
        }
    ]
    assert captured["metadata"]["runtime_event_type"] == "hook_progress"
    assert captured["metadata"]["hook_event"] == "PreToolUse"


def test_delegate_tool_result_builds_runtime_action_started_event():
    import json

    import app.services.web_chat_runtime as runtime

    event = runtime._runtime_action_event_from_tool_result(
        {
            "name": "delegate_to_agent",
            "status": "done",
            "args": {"agent_name": "Web3研究员"},
            "result": json.dumps(
                {
                    "runtime_task_id": "task-1",
                    "child_session_id": "child-1",
                    "session_id": "child-1",
                    "status": "running",
                },
                ensure_ascii=False,
            ),
            "runtime_task_id": "root-run",
            "parent_session_id": "parent-1",
        }
    )

    assert event == {
        "type": "runtime_action_started",
        "message": "已委派给 Web3研究员，后台执行中。",
        "status": "running",
        "action_kind": "a2a_delegation",
        "tool_name": "delegate_to_agent",
        "target_agent_name": "Web3研究员",
        "runtime_task_id": "task-1",
        "child_session_id": "child-1",
        "session_id": "child-1",
        "parent_session_id": "parent-1",
        "notification_source": "a2a",
    }


def test_spawn_subagent_tool_result_builds_runtime_action_started_event():
    import json

    import app.services.web_chat_runtime as runtime

    event = runtime._runtime_action_event_from_tool_result(
        {
            "name": "spawn_subagent",
            "status": "done",
            "args": {"name": "scout", "run_in_background": True},
            "result": json.dumps(
                {
                    "ok": True,
                    "mode": "background",
                    "run_id": "subagent-run-1",
                    "child_session_id": "child-session-1",
                    "subagent": "scout",
                    "status": "running",
                    "return_contract": "background_completion_wake",
                    "subagent_return_contract": {
                        "schema": "hive.ccplus.subagent_return_contract.v1",
                        "return_contract": "background_completion_wake",
                        "normal_wait_path": "completion_wake",
                        "fallback_tool": "check_subagent",
                        "busy_poll_allowed": False,
                    },
                },
                ensure_ascii=False,
            ),
            "runtime_task_id": "root-run",
            "parent_session_id": "parent-1",
        }
    )

    assert event == {
        "type": "runtime_action_started",
        "message": "Subagent scout is running in the background.",
        "status": "running",
        "action_kind": "subagent",
        "tool_name": "spawn_subagent",
        "target_agent_name": "scout",
        "runtime_task_id": "subagent-run-1",
        "child_session_id": "child-session-1",
        "session_id": "child-session-1",
        "parent_session_id": "parent-1",
        "notification_source": "subagent",
        "return_contract": "background_completion_wake",
        "subagent_return_contract": {
            "schema": "hive.ccplus.subagent_return_contract.v1",
            "return_contract": "background_completion_wake",
            "normal_wait_path": "completion_wake",
            "fallback_tool": "check_subagent",
            "busy_poll_allowed": False,
        },
    }


@pytest.mark.asyncio
async def test_session_context_runtime_event_persists_as_specific_context_event(monkeypatch):
    import app.services.web_chat_runtime as runtime
    import app.services.tenant_resolver as tenant_resolver

    captured: dict = {}
    tenant_id = uuid4()

    class _SessionCtx:
        async def __aenter__(self):
            return _FakeDB()

        async def __aexit__(self, *_exc):
            return False

    async def fake_resolve_tenant_for_agent(_agent_id):
        return tenant_id

    async def fake_append_session_event(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(event_id=uuid4(), sequence=1, message_id=None)

    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda _tenant_id: _SessionCtx())
    monkeypatch.setattr(runtime, "append_session_event", fake_append_session_event)

    data = {
        "type": "session_context",
        "event_type": "context_window_status",
        "active_context_tokens": 120,
        "auto_compact_scope_limit": 223000,
        "tokens_until_compaction": 222880,
        "runtime_task_id": str(uuid4()),
        "visibility": "debug",
    }

    assert runtime._should_persist_runtime_event(data)

    await runtime._persist_runtime_event(
        agent_id=uuid4(),
        user_id=uuid4(),
        session_id=str(uuid4()),
        data=data,
    )

    assert captured["event_type"] == "context_window_status"
    assert captured["metadata"]["runtime_event_type"] == "context_window_status"
    assert captured["metadata"]["active_context_tokens"] == 120
    assert captured["metadata"]["visibility"] == "debug"
    assert captured["parts"] is None


def test_plan_mode_unsubmitted_terminal_error_blocks_plain_assistant_completion():
    import app.services.web_chat_runtime as runtime
    from app.runtime.session import PlanModeState, SessionContext

    context = SessionContext(session_id="session-1", source="web_chat", channel="web")
    context.plan_mode = PlanModeState(
        active=True,
        original_request="draft a plan",
        intent_type="in_session_execution",
        action_kind="continue_current_session",
        tool_name="continue_current_session",
    )

    message = runtime._plan_mode_unsubmitted_terminal_error(context)

    assert message is not None
    assert "ask_user_question" in message
    assert "exit_plan_mode" in message


def test_explicit_plan_mode_request_does_not_clear_existing_plan_state_before_reactivation():
    import app.services.web_chat_runtime as runtime
    from app.runtime.session import PlanModeState, SessionContext

    context = SessionContext(session_id="session-1", source="web_chat", channel="web")
    context.plan_mode = PlanModeState(
        active=True,
        original_request="draft a plan",
        intent_type="in_session_execution",
        action_kind="start_long_task",
        tool_name="continue_current_session",
    )
    context.metadata["plan_mode"] = context.plan_mode.to_metadata()

    runtime._clear_stale_plan_mode_for_new_turn(
        context,
        plan_mode_requested=True,
        history_messages=[],
    )

    assert context.plan_mode.active is True
    assert context.metadata["plan_mode"]["active"] is True


def test_record_skill_runtime_usage_for_invocation_collects_web_chat_loaded_skill(monkeypatch, tmp_path):
    import app.services.skill_runtime_telemetry as telemetry
    from app.runtime.session import SessionContext

    agent_id = uuid4()
    calls = []

    monkeypatch.setattr(telemetry, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    def fake_record_skill_runtime_usage(workspace, **kwargs):
        calls.append((workspace, kwargs))
        return {"decision": "promote_candidate", "workflow_signature": "load_skill -> web_search"}

    monkeypatch.setattr(telemetry, "record_skill_runtime_usage", fake_record_skill_runtime_usage)

    result = telemetry.record_skill_runtime_usage_for_invocation(
        agent_id=agent_id,
        session_context=SessionContext(session_id="session-1", source="web_chat", channel="web"),
        tool_events=[
            {"name": "load_skill", "args": {"name": "Deployment Review"}, "status": "done"},
            {"name": "web_search", "args": {"query": "railway logs"}, "status": "done"},
        ],
        terminal_status="completed",
        assistant_text="Finished production deployment review.",
        note="Finished production deployment review.",
    )

    assert result == {"decision": "promote_candidate", "workflow_signature": "load_skill -> web_search"}
    assert calls == [
        (
            tmp_path / str(agent_id),
            {
                "skill_name": "Deployment Review",
                "loaded_skill_names": ["Deployment Review"],
                "tool_names": ["load_skill", "web_search"],
                "status": "unknown",
                "note": "Finished production deployment review.",
                "source": "web_chat",
                "session_id": "session-1",
                "runtime_task_id": None,
                "trace_id": None,
                "blocker": None,
            },
        )
    ]


@pytest.mark.asyncio
async def test_cancel_web_chat_run_compatibility_facade_uses_canonical_control_input(monkeypatch):
    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    tenant_id = uuid4()
    agent_id = uuid4()
    session_id = uuid4()
    user_id = uuid4()
    runtime_task = SimpleNamespace(
        id=run_id,
        task_type="web_chat_turn",
        tenant_id=tenant_id,
        status="running",
        parent_agent_id=agent_id,
        parent_session_id=str(session_id),
        metadata_json={"user_id": str(user_id), "session_id": str(session_id)},
        result_summary=None,
        completed_at=None,
    )
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    user = SimpleNamespace(id=user_id, tenant_id=tenant_id)
    session = SimpleNamespace(id=session_id, tenant_id=tenant_id, agent_id=agent_id)
    calls = []

    class _CanonicalDB(_FakeDB):
        def __init__(self):
            super().__init__(active_run=runtime_task)
            self.rows = [agent, user, session]

        async def scalar(self, _stmt):
            return self.rows.pop(0)

        async def get(self, _model, _key):
            return runtime_task

    async def fake_submit_live_cancel_input(**kwargs):
        calls.append(kwargs)
        return {"schema": "hive.control_input_receipt", "status": "applying"}

    monkeypatch.setattr("app.services.session_live_input.submit_live_cancel_input", fake_submit_live_cancel_input)
    db = _CanonicalDB()
    result = await runtime.cancel_web_chat_run(
        db=db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        user_id=user_id,
    )

    assert result["run_id"] == run_id.hex
    assert result["status"] == "running"
    assert result["control_input"]["status"] == "applying"
    assert runtime_task.status == "running"
    assert calls[0]["agent"] is agent
    assert calls[0]["user"] is user
    assert calls[0]["session"] is session
    assert calls[0]["run_id"] == run_id


@pytest.mark.asyncio
async def test_cancel_web_chat_run_does_not_broadcast_terminal_before_worker_terminal(monkeypatch):
    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    tenant_id = uuid4()
    agent_id = uuid4()
    session_id = uuid4()
    user_id = uuid4()
    runtime_task = SimpleNamespace(
        id=run_id,
        task_type="web_chat_turn",
        tenant_id=tenant_id,
        status="running",
        parent_agent_id=agent_id,
        parent_session_id=str(session_id),
        metadata_json={"user_id": str(user_id), "session_id": str(session_id)},
        result_summary=None,
        completed_at=None,
        created_at=None,
        started_at=None,
    )
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    user = SimpleNamespace(id=user_id, tenant_id=tenant_id)
    session = SimpleNamespace(id=session_id, tenant_id=tenant_id, agent_id=agent_id)
    broadcasts = []

    class _CanonicalDB(_FakeDB):
        def __init__(self):
            super().__init__(active_run=runtime_task)
            self.rows = [agent, user, session]

        async def scalar(self, _stmt):
            return self.rows.pop(0)

        async def get(self, _model, _key):
            return runtime_task

    async def fake_submit_live_cancel_input(**_kwargs):
        return {"schema": "hive.control_input_receipt", "status": "applying"}

    async def fake_broadcast(*args, **kwargs):
        broadcasts.append((args, kwargs))

    monkeypatch.setattr("app.services.session_live_input.submit_live_cancel_input", fake_submit_live_cancel_input)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)
    result = await runtime.cancel_web_chat_run(
        db=_CanonicalDB(),
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        user_id=user_id,
    )

    assert result["status"] == "running"
    assert broadcasts == []


@pytest.mark.asyncio
async def test_cancel_web_chat_run_propagates_canonical_adapter_failure_without_terminal_mutation(monkeypatch):
    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    tenant_id = uuid4()
    agent_id = uuid4()
    session_id = uuid4()
    user_id = uuid4()
    runtime_task = SimpleNamespace(
        id=run_id,
        task_type="web_chat_turn",
        tenant_id=tenant_id,
        status="running",
        parent_agent_id=agent_id,
        parent_session_id=str(session_id),
        metadata_json={"user_id": str(user_id), "session_id": str(session_id)},
        result_summary=None,
        completed_at=None,
    )
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    user = SimpleNamespace(id=user_id, tenant_id=tenant_id)
    session = SimpleNamespace(id=session_id, tenant_id=tenant_id, agent_id=agent_id)

    class _CanonicalDB(_FakeDB):
        def __init__(self):
            super().__init__(active_run=runtime_task)
            self.rows = [agent, user, session]

        async def scalar(self, _stmt):
            return self.rows.pop(0)

        async def get(self, _model, _key):
            return runtime_task

    async def fail_canonical_adapter(**_kwargs):
        raise RuntimeError("durable commit failed")

    monkeypatch.setattr("app.services.session_live_input.submit_live_cancel_input", fail_canonical_adapter)
    with pytest.raises(RuntimeError, match="durable commit failed"):
        await runtime.cancel_web_chat_run(
            db=_CanonicalDB(),
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            user_id=user_id,
        )

    assert runtime_task.status == "running"


@pytest.mark.asyncio
async def test_persist_assistant_message_stores_thinking_signature(monkeypatch):
    import app.services.tenant_resolver as tenant_resolver
    import app.services.web_chat_runtime as runtime

    agent_id = uuid4()
    tenant_id = uuid4()
    added = []

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def add(self, value):
            added.append(value)

        async def commit(self):
            return None

    async def fake_resolve_tenant_for_agent(_agent_id):
        return tenant_id

    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda _tenant_id: _Session())

    await runtime._persist_assistant_message(
        agent_id=agent_id,
        user_id=uuid4(),
        session_id=uuid4().hex,
        content="answer",
        thinking="private thinking",
        thinking_signature="sig-db",
    )

    assert added[0].tenant_id == tenant_id
    assert added[0].thinking == "private thinking"
    assert added[0].thinking_signature == "sig-db"


@pytest.mark.asyncio
async def test_finalize_web_chat_run_skips_assistant_when_run_already_terminal(monkeypatch):
    import app.services.tenant_resolver as tenant_resolver
    import app.services.web_chat_runtime as runtime

    agent_id = uuid4()
    tenant_id = uuid4()
    run_id = uuid4()
    task = SimpleNamespace(
        id=run_id,
        task_type="web_chat_turn",
        status="completed",
        metadata_json={},
        result_summary="already done",
        completed_at=None,
    )
    added = []

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _stmt):
            return _ScalarResult(task)

        def add(self, value):
            added.append(value)

        async def commit(self):
            raise AssertionError("terminal duplicate finalization must not commit")

    async def fake_resolve_tenant_for_agent(_agent_id):
        return tenant_id

    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda _tenant_id: _Session())

    finalized = await runtime._finalize_web_chat_run_with_assistant(
        run_uuid=run_id,
        agent_id=agent_id,
        user_id=uuid4(),
        session_id=uuid4().hex,
        content="duplicate answer",
        thinking=None,
        thinking_signature=None,
        status="completed",
        result_summary="duplicate answer",
        metadata_json={"cancelled_by_user": False},
    )

    assert finalized is False
    assert added == []


@pytest.mark.asyncio
async def test_finalize_web_chat_run_sets_run_scoped_assistant_marker(monkeypatch, tmp_path):
    import app.services.tenant_resolver as tenant_resolver
    import app.services.web_chat_runtime as runtime

    agent_id = uuid4()
    tenant_id = uuid4()
    run_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    task = SimpleNamespace(
        id=run_id,
        task_type="web_chat_turn",
        status="running",
        metadata_json={},
        result_summary=None,
        completed_at=None,
    )
    added = []

    class _Session:
        def __init__(self):
            self.results = [task, None, None]
            self.commits = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _stmt):
            return _ScalarResult(self.results.pop(0))

        def add(self, value):
            added.append(value)

        async def flush(self):
            return None

        async def commit(self):
            self.commits += 1

    session = _Session()

    async def fake_resolve_tenant_for_agent(_agent_id):
        return tenant_id

    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda _tenant_id: session)
    monkeypatch.setattr(runtime, "_enqueue_terminal_channel_delivery", _noop_async)
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    finalized = await runtime._finalize_web_chat_run_with_assistant(
        run_uuid=run_id,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        content="final answer",
        thinking="private reasoning",
        thinking_signature="sig-final",
        status="completed",
        result_summary="final answer",
        metadata_json={"cancelled_by_user": False},
    )

    assert finalized is True
    chat_messages = [item for item in added if getattr(item, "role", None) == "assistant"]
    transcript_events = [item for item in added if getattr(item, "event_type", None) == "assistant_message"]
    assert len(chat_messages) == 1
    assert len(transcript_events) == 1
    assert chat_messages[0].decision_trace_id == f"web_chat_final:{run_id.hex}"
    assert chat_messages[0].thinking == "private reasoning"
    assert chat_messages[0].thinking_signature == "sig-final"
    assert transcript_events[0].parts_json[:2] == [
        {"type": "reasoning", "text": "private reasoning"},
        {"type": "text", "text": "final answer"},
    ]
    assert task.status == "completed"
    assert task.result_summary == "final answer"
    assert session.commits == 1
    assert transcript_events[0].projection_status == "pending"
    assert transcript_events[0].run_id == run_id
    assert transcript_events[0].metadata_json["t0_bridge_pending"] is True


@pytest.mark.asyncio
async def test_finalize_web_chat_run_binds_recent_workspace_artifacts(monkeypatch, tmp_path):
    import app.services.tenant_resolver as tenant_resolver
    import app.services.web_chat_runtime as runtime

    agent_id = uuid4()
    tenant_id = uuid4()
    run_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    task = SimpleNamespace(
        id=run_id,
        task_type="web_chat_turn",
        status="running",
        metadata_json={},
        result_summary=None,
        completed_at=None,
    )
    added = []
    artifact_calls = []

    class _Session:
        def __init__(self):
            self.results = [task, None, None]
            self.commits = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _stmt):
            return _ScalarResult(self.results.pop(0))

        def add(self, value):
            added.append(value)

        async def flush(self):
            return None

        async def commit(self):
            self.commits += 1

    session = _Session()

    async def fake_resolve_tenant_for_agent(_agent_id):
        return tenant_id

    async def fake_create_chat_artifacts_for_message(**kwargs):
        artifact_calls.append(kwargs)
        materialized_messages = [item for item in added if getattr(item, "role", None) == "assistant"]
        assert materialized_messages
        assert kwargs["message_id"] == materialized_messages[0].id
        return [
            {
                "artifact_id": "artifact-1",
                "path": "workspace/report.md",
                "name": "report.md",
                "preview_kind": "markdown",
                "source": "workspace_write",
            }
        ]

    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda _tenant_id: session)
    monkeypatch.setattr(runtime, "_enqueue_terminal_channel_delivery", _noop_async)
    monkeypatch.setattr(runtime, "create_chat_artifacts_for_message", fake_create_chat_artifacts_for_message)
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    metadata_update = {"cancelled_by_user": False}
    finalized = await runtime._finalize_web_chat_run_with_assistant(
        run_uuid=run_id,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        content="final answer",
        thinking=None,
        status="completed",
        result_summary="final answer",
        metadata_json=metadata_update,
        artifact_paths=["workspace/report.md"],
    )

    assert finalized is True
    chat_messages = [item for item in added if getattr(item, "role", None) == "assistant"]
    transcript_events = [item for item in added if getattr(item, "event_type", None)]
    assert len(chat_messages) == 1
    assert [event.event_type for event in transcript_events] == ["assistant_message", "artifact_delivery"]
    assert artifact_calls
    assert artifact_calls[0]["message_id"] == chat_messages[0].id
    assert artifact_calls[0]["runtime_task_id"] == run_id
    assert artifact_calls[0]["paths"] == ["workspace/report.md"]
    assert task.metadata_json["artifact_ids"] == ["artifact-1"]
    assert metadata_update["artifact_ids"] == ["artifact-1"]
    assert metadata_update["artifacts"][0]["path"] == "workspace/report.md"
    assert all(event.projection_status == "pending" for event in transcript_events)


@pytest.mark.asyncio
async def test_finalize_web_chat_run_records_file_changes_side_channel(monkeypatch, tmp_path):
    import app.services.tenant_resolver as tenant_resolver
    import app.services.web_chat_runtime as runtime

    agent_id = uuid4()
    tenant_id = uuid4()
    run_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    task = SimpleNamespace(
        id=run_id,
        task_type="web_chat_turn",
        status="running",
        metadata_json={},
        result_summary=None,
        completed_at=None,
    )
    added = []
    artifact_calls = []

    class _Session:
        def __init__(self):
            self.results = [task, None, None]
            self.commits = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _stmt):
            return _ScalarResult(self.results.pop(0))

        def add(self, value):
            added.append(value)

        async def flush(self):
            return None

        async def commit(self):
            self.commits += 1

    session = _Session()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "report.md").write_text("report", encoding="utf-8")
    (workspace / "scratch.md").write_text("scratch", encoding="utf-8")
    exact_write_states = {
        "workspace/report.md": {
            "path": "workspace/report.md",
            "exists": True,
            "sha256": "c" * 64,
            "size": 6,
        },
        "workspace/scratch.md": {
            "path": "workspace/scratch.md",
            "exists": True,
            "sha256": "d" * 64,
            "size": 7,
        },
    }
    exact_lineage = [
        {
            "path": path,
            "before_state": {"path": path, "exists": False, "sha256": None, "size": 0},
            "after_state": state,
        }
        for path, state in exact_write_states.items()
    ]

    async def fake_resolve_tenant_for_agent(_agent_id):
        return tenant_id

    async def fake_create_chat_artifacts_for_message(**kwargs):
        artifact_calls.append(kwargs)
        return [
            {
                "artifact_id": "artifact-1",
                "path": "workspace/report.md",
                "name": "report.md",
                "preview_kind": "markdown",
                "source": "workspace_write",
            }
        ]

    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda _tenant_id: session)
    monkeypatch.setattr(runtime, "_enqueue_terminal_channel_delivery", _noop_async)
    monkeypatch.setattr(runtime, "create_chat_artifacts_for_message", fake_create_chat_artifacts_for_message)
    monkeypatch.setattr(runtime, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    finalized = await runtime._finalize_web_chat_run_with_assistant(
        run_uuid=run_id,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        content="完成。\nDELIVERABLE: workspace/report.md\nDELIVERABLE: workspace/stale.md",
        thinking=None,
        status="completed",
        result_summary="完成。",
        metadata_json={"cancelled_by_user": False},
        artifact_paths=["workspace/report.md"],
        file_change_paths=["workspace/report.md", "workspace/scratch.md"],
        file_change_states=exact_write_states,
        file_change_lineage=exact_lineage,
        rejected_artifact_paths=["workspace/stale.md"],
    )

    assert finalized is True
    transcript_events = [item for item in added if getattr(item, "event_type", None)]
    assert [event.event_type for event in transcript_events] == [
        "assistant_message",
        "artifact_delivery",
        "file_changes",
    ]
    assert artifact_calls[0]["paths"] == ["workspace/report.md"]
    file_change_event = transcript_events[-1]
    assert file_change_event.content == "file_changes"
    assert file_change_event.metadata_json["file_change_paths"] == ["workspace/report.md", "workspace/scratch.md"]
    assert file_change_event.metadata_json["file_change_states"]["workspace/report.md"]["exists"] is True
    assert file_change_event.metadata_json["file_change_states"] == exact_write_states
    assert file_change_event.metadata_json["file_change_lineage"] == exact_lineage
    assert file_change_event.metadata_json["attached_artifact_paths"] == ["workspace/report.md"]
    assert file_change_event.metadata_json["rejected_artifact_paths"] == ["workspace/stale.md"]
    assert task.metadata_json["file_change_paths"] == ["workspace/report.md", "workspace/scratch.md"]
    assert task.metadata_json["rejected_artifact_paths"] == ["workspace/stale.md"]

    assert all(event.projection_status == "pending" for event in transcript_events)


@pytest.mark.asyncio
async def test_file_change_side_channel_emits_file_and_artifact_hooks(monkeypatch):
    import app.services.web_chat_runtime as runtime
    from app.runtime.hooks import HookResult

    tenant_id = uuid4()
    agent_id = uuid4()
    run_id = uuid4()
    session_id = uuid4()
    captured_hooks = []
    persisted = []

    async def fake_emit(event, **kwargs):
        captured_hooks.append((event.value, kwargs))
        if event.value == "file_changed":
            return HookResult(watch_paths=["workspace/**/*.md"])
        return None

    async def fake_append(**kwargs):
        persisted.append(kwargs)
        return SimpleNamespace(event_id=uuid4(), sequence=1, message_id=None)

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit)
    monkeypatch.setattr(runtime, "append_session_event", fake_append)
    await runtime._append_file_changes_event(
        db=SimpleNamespace(),
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=str(session_id),
        run_uuid=run_id,
        message_id=None,
        file_change_paths=["workspace/report.md"],
        file_change_states={
            "workspace/report.md": {"path": "workspace/report.md", "exists": True, "sha256": "a" * 64, "size": 6}
        },
        file_change_lineage=[
            {
                "path": "workspace/report.md",
                "before_state": {"exists": False, "sha256": None, "size": 0},
                "after_state": {"exists": True, "sha256": "a" * 64, "size": 6},
            }
        ],
        attached_artifact_paths=["workspace/report.md"],
        declared_artifact_paths=["workspace/report.md", "workspace/missing.md"],
        rejected_artifact_paths=["workspace/missing.md"],
    )

    assert [name for name, _kwargs in captured_hooks] == ["file_changed", "artifact_changed"]
    assert captured_hooks[0][1]["metadata"]["change_kind"] == "add"
    assert captured_hooks[0][1]["metadata"]["file_path"] == "workspace/report.md"
    assert captured_hooks[1][1]["metadata"]["attached_artifact_paths"] == ["workspace/report.md"]
    assert persisted[0]["metadata"]["hook_watch_paths"] == ["workspace/**/*.md"]


@pytest.mark.asyncio
async def test_tool_card_finalization_preserves_exact_file_change_evidence(monkeypatch):
    import app.services.tenant_resolver as tenant_resolver
    import app.services.web_chat_runtime as runtime

    agent_id = uuid4()
    tenant_id = uuid4()
    run_id = uuid4()
    session_id = uuid4().hex
    task = SimpleNamespace(
        id=run_id,
        task_type="web_chat_turn",
        parent_session_id=session_id,
        status="running",
        metadata_json={},
        result_summary=None,
        completed_at=None,
    )
    appended: list[dict] = []

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _stmt):
            return _ScalarResult(task)

        async def commit(self):
            return None

    async def fake_resolve_tenant_for_agent(_agent_id):
        return tenant_id

    async def capture_file_changes(**kwargs):
        appended.append(kwargs)

    async def noop_async(**_kwargs):
        return None

    exact = {
        "workspace/report.md": {
            "path": "workspace/report.md",
            "exists": True,
            "sha256": "1" * 64,
            "size": 8,
        }
    }
    exact_lineage = [
        {
            "path": "workspace/report.md",
            "before_state": {
                "path": "workspace/report.md",
                "exists": False,
                "sha256": None,
                "size": 0,
            },
            "after_state": exact["workspace/report.md"],
        }
    ]
    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda _tenant_id: _Session())
    monkeypatch.setattr(runtime, "_append_file_changes_event", capture_file_changes)
    monkeypatch.setattr(runtime, "_project_agent_team_terminal_state", noop_async)
    monkeypatch.setattr(runtime, "_maybe_continue_goal_after_terminal_turn", noop_async)

    finalized = await runtime._finalize_web_chat_run_without_assistant(
        run_uuid=run_id,
        agent_id=agent_id,
        session_id=session_id,
        status="completed",
        result_summary="awaiting_user_clarification",
        file_change_paths=["workspace/report.md"],
        file_change_states=exact,
        file_change_lineage=exact_lineage,
    )

    assert finalized is True
    assert task.metadata_json["file_change_states"] == exact
    assert appended[0]["file_change_states"] == exact
    assert appended[0]["file_change_lineage"] == exact_lineage
    assert appended[0]["session_id"] == session_id


@pytest.mark.asyncio
async def test_finalize_web_chat_run_reuses_kernel_persisted_terminal_message(monkeypatch):
    import app.services.tenant_resolver as tenant_resolver
    import app.services.web_chat_runtime as runtime

    agent_id = uuid4()
    user_id = uuid4()
    tenant_id = uuid4()
    run_id = uuid4()
    session_id = uuid4().hex
    created_at = datetime.now(timezone.utc)
    task = SimpleNamespace(
        id=run_id,
        task_type="web_chat_turn",
        status="running",
        metadata_json={},
        result_summary=None,
        created_at=created_at,
        started_at=created_at,
        completed_at=None,
    )
    kernel_message = SimpleNamespace(
        decision_trace_id=None,
        content="budget stopped",
        created_at=created_at,
    )
    added = []

    class _Session:
        def __init__(self):
            self.results = [task, None, kernel_message]
            self.commits = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _stmt):
            return _ScalarResult(self.results.pop(0))

        def add(self, value):
            added.append(value)

        async def commit(self):
            self.commits += 1

    session = _Session()

    async def fake_resolve_tenant_for_agent(_agent_id):
        return tenant_id

    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda _tenant_id: session)
    monkeypatch.setattr(runtime, "_enqueue_terminal_channel_delivery", _noop_async)

    finalized = await runtime._finalize_web_chat_run_with_assistant(
        run_uuid=run_id,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        content="budget stopped",
        thinking=None,
        thinking_signature=None,
        status="completed",
        result_summary="budget stopped",
        metadata_json={"cancelled_by_user": False},
    )

    assert finalized is True
    assert not [item for item in added if getattr(item, "role", None) == "assistant"]
    assert [item.event_type for item in added if getattr(item, "event_type", None)] == ["assistant_message"]
    assert kernel_message.decision_trace_id == f"web_chat_final:{run_id.hex}"
    assert task.status == "completed"
    assert task.result_summary == "budget stopped"
    assert task.completed_at is not None
    assert session.commits == 1


@pytest.mark.asyncio
async def test_finalize_web_chat_run_skips_existing_final_assistant_marker(monkeypatch):
    import app.services.tenant_resolver as tenant_resolver
    import app.services.web_chat_runtime as runtime

    agent_id = uuid4()
    tenant_id = uuid4()
    run_id = uuid4()
    task = SimpleNamespace(
        id=run_id,
        task_type="web_chat_turn",
        status="running",
        metadata_json={},
        result_summary=None,
        completed_at=None,
    )
    existing_message = SimpleNamespace(id=uuid4(), decision_trace_id=f"web_chat_final:{run_id.hex}")
    added = []

    class _Session:
        def __init__(self):
            self.results = [task, existing_message]
            self.commits = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _stmt):
            return _ScalarResult(self.results.pop(0))

        def add(self, value):
            added.append(value)

        async def commit(self):
            self.commits += 1

    session = _Session()

    async def fake_resolve_tenant_for_agent(_agent_id):
        return tenant_id

    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda _tenant_id: session)
    monkeypatch.setattr(runtime, "_enqueue_terminal_channel_delivery", _noop_async)

    finalized = await runtime._finalize_web_chat_run_with_assistant(
        run_uuid=run_id,
        agent_id=agent_id,
        user_id=uuid4(),
        session_id=uuid4().hex,
        content="duplicate final answer",
        thinking=None,
        thinking_signature=None,
        status="completed",
        result_summary="duplicate final answer",
        metadata_json={"cancelled_by_user": False},
    )

    assert finalized is False
    assert added == []
    assert task.status == "completed"
    assert task.result_summary == "duplicate final answer"
    assert task.completed_at is not None
    assert session.commits == 1


@pytest.mark.asyncio
async def test_execute_web_chat_run_does_not_broadcast_done_when_finalization_loses_race(monkeypatch):
    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    runtime_task = SimpleNamespace(
        id=run_id,
        parent_agent_id=agent_id,
        parent_session_id=session_id,
        prompt="latest question",
        metadata_json={"user_id": str(user_id), "session_id": session_id},
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="Web3研究员",
        role_description="",
        primary_model_id=uuid4(),
        fallback_model_id=None,
        tenant_id=uuid4(),
        agent_type="native",
    )
    user = SimpleNamespace(id=user_id, display_name="Rocky", username="rocky")
    llm_model = SimpleNamespace(provider="openai", model="gpt-4.1", supports_vision=False)
    broadcasts: list[dict] = []

    async def fake_load_context(_run_uuid):
        return runtime_task, agent, user, llm_model, None, []

    async def fake_invoke(_request):
        return SimpleNamespace(content="same final answer", reasoning_signature=None)

    async def fake_finalize(**_kwargs):
        return False

    async def fake_broadcast(_agent_id, _session_id, event):
        broadcasts.append(event)

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_load_runtime_context", fake_load_context)
    monkeypatch.setattr(runtime, "_maybe_handle_plan_mode_entry", noop_async)
    monkeypatch.setattr(runtime, "invoke_agent", fake_invoke)
    monkeypatch.setattr(runtime, "_finalize_web_chat_run_with_assistant", fake_finalize)
    monkeypatch.setattr(runtime, "_persist_runtime_event", noop_async)
    monkeypatch.setattr(runtime, "_persist_tool_call", noop_async)
    monkeypatch.setattr(runtime, "_update_runtime_task", noop_async)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)
    monkeypatch.setattr(runtime, "_resume_queued_plan_handoffs", noop_async)

    await runtime.execute_web_chat_run(run_id)

    assert all(event.get("type") != "done" for event in broadcasts)


@pytest.mark.asyncio
async def test_execute_web_chat_run_marks_terminal_persistence_error_when_finalization_raises(monkeypatch):
    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    runtime_task = SimpleNamespace(
        id=run_id,
        parent_agent_id=agent_id,
        parent_session_id=session_id,
        prompt="latest question",
        metadata_json={"user_id": str(user_id), "session_id": session_id},
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="Web3研究员",
        role_description="",
        primary_model_id=uuid4(),
        fallback_model_id=None,
        tenant_id=uuid4(),
        agent_type="native",
    )
    user = SimpleNamespace(id=user_id, display_name="Rocky", username="rocky")
    llm_model = SimpleNamespace(provider="openai", model="gpt-4.1", supports_vision=False)
    task_updates: list[dict] = []
    broadcasts: list[dict] = []

    async def fake_load_context(_run_uuid):
        return runtime_task, agent, user, llm_model, None, []

    async def fake_invoke(_request):
        return SimpleNamespace(content="same final answer", reasoning_signature=None)

    async def fail_finalize(**_kwargs):
        raise RuntimeError("db commit failed")

    async def fake_update_runtime_task(*_args, **kwargs):
        task_updates.append(kwargs)

    async def fake_broadcast(_agent_id, _session_id, event):
        broadcasts.append(event)

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_load_runtime_context", fake_load_context)
    monkeypatch.setattr(runtime, "_maybe_handle_plan_mode_entry", noop_async)
    monkeypatch.setattr(runtime, "invoke_agent", fake_invoke)
    monkeypatch.setattr(runtime, "_finalize_web_chat_run_with_assistant", fail_finalize)
    monkeypatch.setattr(runtime, "_persist_runtime_event", noop_async)
    monkeypatch.setattr(runtime, "_persist_tool_call", noop_async)
    monkeypatch.setattr(runtime, "_update_runtime_task", fake_update_runtime_task)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)
    monkeypatch.setattr(runtime, "_resume_queued_plan_handoffs", noop_async)
    monkeypatch.setattr(runtime, "_emit_terminal_turn_hook", noop_async)

    await runtime.execute_web_chat_run(run_id)

    assert task_updates
    assert task_updates[-1]["status"] == "failed"
    assert task_updates[-1]["metadata_json"]["terminal_reason"] == "persistence_error"
    assert task_updates[-1]["metadata_json"]["persistence_error"] is True
    assert all(event.get("type") != "done" for event in broadcasts)


@pytest.mark.asyncio
async def test_execute_web_chat_run_treats_final_marker_unique_violation_as_lost_race(monkeypatch):
    import app.services.web_chat_runtime as runtime
    from sqlalchemy.exc import IntegrityError

    run_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    runtime_task = SimpleNamespace(
        id=run_id,
        parent_agent_id=agent_id,
        parent_session_id=session_id,
        prompt="latest question",
        metadata_json={"user_id": str(user_id), "session_id": session_id},
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="Web3研究员",
        role_description="",
        primary_model_id=uuid4(),
        fallback_model_id=None,
        tenant_id=uuid4(),
        agent_type="native",
    )
    user = SimpleNamespace(id=user_id, display_name="Rocky", username="rocky")
    llm_model = SimpleNamespace(provider="openai", model="gpt-4.1", supports_vision=False)
    task_updates: list[dict] = []
    broadcasts: list[dict] = []

    class _Orig:
        diag = SimpleNamespace(constraint_name="uq_chat_messages_web_chat_final_decision_trace")

    async def fake_load_context(_run_uuid):
        return runtime_task, agent, user, llm_model, None, []

    async def fake_invoke(_request):
        return SimpleNamespace(content="same final answer", reasoning_signature=None)

    async def fail_finalize(**_kwargs):
        raise IntegrityError("insert chat_messages", {}, _Orig())

    async def fake_update_runtime_task(*_args, **kwargs):
        task_updates.append(kwargs)

    async def fake_broadcast(_agent_id, _session_id, event):
        broadcasts.append(event)

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_load_runtime_context", fake_load_context)
    monkeypatch.setattr(runtime, "_maybe_handle_plan_mode_entry", noop_async)
    monkeypatch.setattr(runtime, "invoke_agent", fake_invoke)
    monkeypatch.setattr(runtime, "_finalize_web_chat_run_with_assistant", fail_finalize)
    monkeypatch.setattr(runtime, "_persist_runtime_event", noop_async)
    monkeypatch.setattr(runtime, "_persist_tool_call", noop_async)
    monkeypatch.setattr(runtime, "_update_runtime_task", fake_update_runtime_task)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)
    monkeypatch.setattr(runtime, "_resume_queued_plan_handoffs", noop_async)
    monkeypatch.setattr(runtime, "_emit_terminal_turn_hook", noop_async)

    await runtime.execute_web_chat_run(run_id)

    assert task_updates == []
    assert all(event.get("type") != "done" for event in broadcasts)


@pytest.mark.asyncio
async def test_execute_web_chat_run_disables_tools_for_side_question(monkeypatch):
    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    runtime_task = SimpleNamespace(
        id=run_id,
        parent_agent_id=agent_id,
        parent_session_id=session_id,
        prompt="side question",
        metadata_json={
            "user_id": str(user_id),
            "session_id": session_id,
            "side_session": True,
            "tool_policy": "disabled_by_default",
            "disable_tools": True,
        },
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="Web3研究员",
        role_description="",
        primary_model_id=uuid4(),
        fallback_model_id=None,
        tenant_id=uuid4(),
        agent_type="native",
    )
    user = SimpleNamespace(id=user_id, display_name="Rocky", username="rocky")
    llm_model = SimpleNamespace(provider="openai", model="gpt-4.1", supports_vision=False)
    captured = {}

    async def fake_load_context(_run_uuid):
        return runtime_task, agent, user, llm_model, None, []

    async def fake_invoke(request):
        captured["disable_tools"] = request.disable_tools
        return SimpleNamespace(content="side answer", reasoning_signature=None)

    async def fake_finalize(**_kwargs):
        return False

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_load_runtime_context", fake_load_context)
    monkeypatch.setattr(runtime, "_maybe_handle_plan_mode_entry", noop_async)
    monkeypatch.setattr(runtime, "invoke_agent", fake_invoke)
    monkeypatch.setattr(runtime, "_finalize_web_chat_run_with_assistant", fake_finalize)
    monkeypatch.setattr(runtime, "_persist_runtime_event", noop_async)
    monkeypatch.setattr(runtime, "_persist_tool_call", noop_async)
    monkeypatch.setattr(runtime, "_update_runtime_task", noop_async)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", noop_async)
    monkeypatch.setattr(runtime, "_resume_queued_plan_handoffs", noop_async)

    await runtime.execute_web_chat_run(run_id)

    assert captured["disable_tools"] is True


@pytest.mark.asyncio
async def test_execute_web_chat_run_keeps_unbound_external_principal_read_only(monkeypatch):
    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    external_principal_id = uuid4()
    session_id = uuid4().hex
    runtime_task = SimpleNamespace(
        id=run_id,
        parent_agent_id=agent_id,
        parent_session_id=session_id,
        prompt="external question",
        metadata_json={
            "user_id": None,
            "external_principal_id": str(external_principal_id),
            "external_authority_bound": False,
            "session_id": session_id,
            "source": "slack",
            "channel": "slack",
        },
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="External Support",
        role_description="",
        primary_model_id=uuid4(),
        fallback_model_id=None,
        tenant_id=uuid4(),
        agent_type="native",
    )
    actor = SimpleNamespace(
        id=None,
        external_principal_id=external_principal_id,
        authority_bound=False,
        display_name="Slack guest",
        username="slack:U1",
    )
    llm_model = SimpleNamespace(provider="openai", model="gpt-4.1", supports_vision=False)
    captured = {}

    async def fake_load_context(_run_uuid):
        return runtime_task, agent, actor, llm_model, None, []

    async def fake_invoke(request):
        captured["request"] = request
        return SimpleNamespace(content="read-only answer", reasoning_signature=None)

    async def fake_finalize(**_kwargs):
        return False

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_load_runtime_context", fake_load_context)
    monkeypatch.setattr(runtime, "_maybe_handle_plan_mode_entry", noop_async)
    monkeypatch.setattr(runtime, "invoke_agent", fake_invoke)
    monkeypatch.setattr(runtime, "_finalize_web_chat_run_with_assistant", fake_finalize)
    monkeypatch.setattr(runtime, "_persist_runtime_event", noop_async)
    monkeypatch.setattr(runtime, "_persist_tool_call", noop_async)
    monkeypatch.setattr(runtime, "_update_runtime_task", noop_async)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", noop_async)
    monkeypatch.setattr(runtime, "_resume_queued_plan_handoffs", noop_async)

    await runtime.execute_web_chat_run(run_id)

    request = captured["request"]
    assert request.user_id is None
    assert request.disable_tools is True
    assert request.execution_identity.identity_type == "external_principal"
    assert request.execution_identity.identity_id == external_principal_id


@pytest.mark.asyncio
async def test_execute_web_chat_run_finalizes_blocking_clarification_without_empty_assistant(monkeypatch):
    import json

    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    runtime_task = SimpleNamespace(
        id=run_id,
        parent_agent_id=agent_id,
        parent_session_id=session_id,
        prompt="Create an RWA monitoring agent",
        metadata_json={"user_id": str(user_id), "session_id": session_id},
        trace_id=None,
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="RWA Researcher",
        role_description="",
        primary_model_id=uuid4(),
        fallback_model_id=None,
        tenant_id=uuid4(),
        agent_type="native",
    )
    user = SimpleNamespace(id=user_id, display_name="Rocky", username="rocky")
    llm_model = SimpleNamespace(provider="openai", model="gpt-4.1", supports_vision=False)
    broadcasts: list[dict] = []
    persisted_tools: list[dict] = []
    ordering: list[str] = []
    finalized_without_assistant: list[dict] = []
    delivered_channel_replies: list[str] = []

    async def fake_load_context(_run_uuid):
        return runtime_task, agent, user, llm_model, None, []

    async def fake_invoke(request):
        await request.on_tool_call(
            {
                "name": "ask_user_question",
                "args": {
                    "questions": [
                        {
                            "question": "How often should the RWA report run?",
                            "options": [{"label": "Weekly", "description": "Weekly report"}],
                        }
                    ]
                },
                "status": "done",
                "result": json.dumps(
                    {
                        "status": "awaiting_user_clarification",
                        "blocking": True,
                        "questions": [
                            {
                                "question": "How often should the RWA report run?",
                                "options": [{"label": "Weekly", "description": "Weekly report"}],
                            }
                        ],
                    }
                ),
            }
        )
        return SimpleNamespace(content="", reasoning_signature=None)

    async def fail_empty_assistant_finalize(**kwargs):
        raise AssertionError(f"blocking clarification must not persist assistant content={kwargs.get('content')!r}")

    async def fake_finalize_without_assistant(**kwargs):
        finalized_without_assistant.append(kwargs)
        return True

    async def fake_broadcast(_agent_id, _session_id, event):
        broadcasts.append(event)
        if event.get("type") == "tool_call":
            ordering.append(f"broadcast:{event.get('name')}")

    async def fake_persist_tool_call(**kwargs):
        persisted_tools.append(kwargs["data"])
        ordering.append(f"persist:{kwargs['data'].get('name')}")
        return [
            {
                "type": "tool_call",
                "name": kwargs["data"].get("name"),
                "visibility": {"audience": "direct_user"},
            }
        ]

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_load_runtime_context", fake_load_context)
    monkeypatch.setattr(runtime, "_maybe_handle_plan_mode_entry", noop_async)
    monkeypatch.setattr(runtime, "_claim_pending_reply_suffix_for_session", noop_async)
    monkeypatch.setattr(runtime, "invoke_agent", fake_invoke)
    monkeypatch.setattr(runtime, "_finalize_web_chat_run_with_assistant", fail_empty_assistant_finalize)
    monkeypatch.setattr(
        runtime, "_finalize_web_chat_run_without_assistant", fake_finalize_without_assistant, raising=False
    )
    monkeypatch.setattr(runtime, "_persist_runtime_event", noop_async)
    monkeypatch.setattr(runtime, "_persist_tool_call", fake_persist_tool_call)
    monkeypatch.setattr(runtime, "_update_runtime_task", noop_async)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)
    monkeypatch.setattr(runtime, "_resume_queued_plan_handoffs", noop_async)

    await runtime.execute_web_chat_run(run_id)

    assert [tool["name"] for tool in persisted_tools] == ["ask_user_question"]
    assert ordering.index("persist:ask_user_question") < ordering.index("broadcast:ask_user_question")
    assert len(finalized_without_assistant) == 1
    assert finalized_without_assistant[0]["status"] == "completed"
    assert finalized_without_assistant[0]["result_summary"] == "awaiting_user_clarification"
    assert any(event.get("type") == "done" and event.get("content") == "" for event in broadcasts)
    assert delivered_channel_replies == []


@pytest.mark.asyncio
async def test_execute_web_chat_run_interrupts_kernel_after_terminal_tool_card(monkeypatch):
    import json

    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    runtime_task = SimpleNamespace(
        id=run_id,
        parent_agent_id=agent_id,
        parent_session_id=session_id,
        prompt="Create an RWA employee",
        metadata_json={"user_id": str(user_id), "session_id": session_id},
        trace_id=None,
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="HR",
        role_description="",
        primary_model_id=uuid4(),
        fallback_model_id=None,
        tenant_id=uuid4(),
        agent_type="native",
    )
    user = SimpleNamespace(id=user_id, display_name="Rocky", username="rocky")
    llm_model = SimpleNamespace(provider="openai", model="gpt-4.1", supports_vision=False)
    broadcasts: list[dict] = []
    finalized_without_assistant: list[dict] = []

    async def fake_load_context(_run_uuid):
        return runtime_task, agent, user, llm_model, None, []

    async def fake_invoke(request):
        await request.on_tool_call(
            {
                "name": "ask_user_question",
                "args": {"questions": [{"question": "Cadence?", "options": [{"label": "Weekly"}]}]},
                "status": "done",
                "result": json.dumps(
                    {
                        "status": "awaiting_user_clarification",
                        "blocking": True,
                        "questions": [{"question": "Cadence?", "options": [{"label": "Weekly"}]}],
                    }
                ),
            }
        )
        raise AssertionError("kernel must be interrupted after a terminal tool card")

    async def fail_assistant_finalize(**kwargs):
        raise AssertionError(f"terminal tool card must not persist assistant content={kwargs.get('content')!r}")

    async def fake_finalize_without_assistant(**kwargs):
        finalized_without_assistant.append(kwargs)
        return True

    async def fake_broadcast(_agent_id, _session_id, event):
        broadcasts.append(event)

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_load_runtime_context", fake_load_context)
    monkeypatch.setattr(runtime, "_maybe_handle_plan_mode_entry", noop_async)
    monkeypatch.setattr(runtime, "invoke_agent", fake_invoke)
    monkeypatch.setattr(runtime, "_finalize_web_chat_run_with_assistant", fail_assistant_finalize)
    monkeypatch.setattr(
        runtime, "_finalize_web_chat_run_without_assistant", fake_finalize_without_assistant, raising=False
    )
    monkeypatch.setattr(runtime, "_persist_runtime_event", noop_async)
    monkeypatch.setattr(runtime, "_persist_tool_call", noop_async)
    monkeypatch.setattr(runtime, "_update_runtime_task", noop_async)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)
    monkeypatch.setattr(runtime, "_resume_queued_plan_handoffs", noop_async)

    await runtime.execute_web_chat_run(run_id)

    assert len(finalized_without_assistant) == 1
    assert finalized_without_assistant[0]["status"] == "completed"
    assert finalized_without_assistant[0]["result_summary"] == "awaiting_user_clarification"
    assert any(event.get("type") == "done" and event.get("content") == "" for event in broadcasts)


@pytest.mark.asyncio
async def test_execute_web_chat_run_delivers_session_permission_prompt_to_channel(monkeypatch):
    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    permission_request_id = uuid4()
    runtime_task = SimpleNamespace(
        id=run_id,
        parent_agent_id=agent_id,
        parent_session_id=session_id,
        prompt="委派给 Researcher",
        metadata_json={
            "user_id": str(user_id),
            "session_id": session_id,
            "source": "telegram",
            "channel": "telegram",
            "permission_mode": "auto",
            "permission_profile": {"mode": "auto", "allowed_tools": [], "writable_roots": ["workspace/"]},
        },
        trace_id=None,
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="Coordinator",
        role_description="",
        primary_model_id=uuid4(),
        fallback_model_id=None,
        tenant_id=uuid4(),
        agent_type="native",
    )
    user = SimpleNamespace(id=user_id, display_name="Rocky", username="rocky")
    llm_model = SimpleNamespace(provider="openai", model="gpt-4.1", supports_vision=False)
    session = SimpleNamespace(delivery_target_json={"channel": "telegram", "chat_id": "100", "sender_id": "200"})
    finalized_without_assistant: list[dict] = []
    runtime_updates: list[dict] = []

    async def fake_load_context(_run_uuid):
        return runtime_task, agent, user, llm_model, None, [], session

    async def fake_invoke(request):
        await request.on_tool_call(
            {
                "name": "send_message_to_agent",
                "args": {"agent_name": "Researcher", "message": "请验证 Web 通道"},
                "status": "done",
                "result": json.dumps(
                    {
                        "status": "session_permission_required",
                        "message": "Tool requires session permission",
                        "permission_request": {
                            "permission_request_id": str(permission_request_id),
                            "tool_name": "send_message_to_agent",
                            "tool_display_name": "Send Message to Agent",
                            "arguments": {"agent_name": "Researcher", "message": "请验证 Web 通道"},
                            "capability": "agent.message.send",
                            "permission_mode": "auto",
                            "decision_reason": "no enterprise capability policy is configured for this tool",
                        },
                    },
                    ensure_ascii=False,
                ),
            }
        )
        return SimpleNamespace(content="", reasoning_signature=None)

    async def fake_finalize_without_assistant(**kwargs):
        finalized_without_assistant.append(kwargs)
        return True

    async def fake_update_runtime_task(_run_uuid, **kwargs):
        runtime_updates.append(kwargs)

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_load_runtime_context", fake_load_context)
    monkeypatch.setattr(runtime, "_maybe_handle_plan_mode_entry", noop_async)
    monkeypatch.setattr(runtime, "invoke_agent", fake_invoke)
    monkeypatch.setattr(
        runtime, "_finalize_web_chat_run_without_assistant", fake_finalize_without_assistant, raising=False
    )
    monkeypatch.setattr(runtime, "_finalize_web_chat_run_with_assistant", noop_async)
    monkeypatch.setattr(runtime, "_claim_pending_reply_suffix_for_session", noop_async)
    monkeypatch.setattr(runtime, "_persist_runtime_event", noop_async)
    monkeypatch.setattr(runtime, "_persist_tool_call", noop_async)
    monkeypatch.setattr(runtime, "_update_runtime_task", fake_update_runtime_task)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", noop_async)
    monkeypatch.setattr(runtime, "_resume_queued_plan_handoffs", noop_async)

    await runtime.execute_web_chat_run(run_id)

    assert finalized_without_assistant == []
    assert len(runtime_updates) == 1
    assert runtime_updates[0]["status"] == "suspended"
    assert runtime_updates[0]["result_summary"] == "awaiting_session_permission"
    delivered_text = runtime_updates[0]["channel_delivery_text"]
    assert "Send Message to Agent" in delivered_text
    assert "允许" in delivered_text
    assert "本会话允许" in delivered_text
    assert "拒绝" in delivered_text
    assert str(permission_request_id) in delivered_text
    assert "enterprise capability policy" not in delivered_text


@pytest.mark.asyncio
async def test_execute_web_chat_run_releases_active_run_inside_terminal_tool_callback(monkeypatch):
    """The user can answer immediately after an ask_user_question card appears.

    The web-chat callback must mark the run terminal before raising its internal
    control signal; otherwise the REST send path sees an active run and queues
    the user's answer behind the same still-running turn.
    """
    import json

    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    runtime_task = SimpleNamespace(
        id=run_id,
        parent_agent_id=agent_id,
        parent_session_id=session_id,
        prompt="Create an analyst",
        metadata_json={"user_id": str(user_id), "session_id": session_id},
        trace_id=None,
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="HR",
        role_description="",
        primary_model_id=uuid4(),
        fallback_model_id=None,
        tenant_id=uuid4(),
        agent_type="native",
    )
    user = SimpleNamespace(id=user_id, display_name="Rocky", username="rocky")
    llm_model = SimpleNamespace(provider="openai", model="gpt-4.1", supports_vision=False)
    finalized_without_assistant: list[dict] = []
    broadcasts: list[dict] = []

    async def fake_load_context(_run_uuid):
        return runtime_task, agent, user, llm_model, None, []

    async def fake_invoke(request):
        try:
            await request.on_tool_call(
                {
                    "name": "ask_user_question",
                    "args": {"questions": [{"question": "Scope?", "options": [{"label": "Mine"}]}]},
                    "status": "done",
                    "result": json.dumps(
                        {
                            "status": "awaiting_user_clarification",
                            "blocking": True,
                            "questions": [{"question": "Scope?", "options": [{"label": "Mine"}]}],
                        }
                    ),
                }
            )
        except runtime._TerminalToolCardSignal:
            assert finalized_without_assistant, "terminal card callback must release the active run before it returns"
            raise
        raise AssertionError("terminal tool card should interrupt invoke_agent")

    async def fake_finalize_without_assistant(**kwargs):
        finalized_without_assistant.append(kwargs)
        return True

    async def fake_broadcast(_agent_id, _session_id, event):
        broadcasts.append(event)

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_load_runtime_context", fake_load_context)
    monkeypatch.setattr(runtime, "_maybe_handle_plan_mode_entry", noop_async)
    monkeypatch.setattr(runtime, "invoke_agent", fake_invoke)
    monkeypatch.setattr(
        runtime, "_finalize_web_chat_run_without_assistant", fake_finalize_without_assistant, raising=False
    )
    monkeypatch.setattr(runtime, "_finalize_web_chat_run_with_assistant", noop_async)
    monkeypatch.setattr(runtime, "_claim_pending_reply_suffix_for_session", noop_async)
    monkeypatch.setattr(runtime, "_persist_runtime_event", noop_async)
    monkeypatch.setattr(runtime, "_persist_tool_call", noop_async)
    monkeypatch.setattr(runtime, "_update_runtime_task", noop_async)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)
    monkeypatch.setattr(runtime, "_resume_queued_plan_handoffs", noop_async)

    await runtime.execute_web_chat_run(run_id)

    assert len(finalized_without_assistant) == 1
    assert finalized_without_assistant[0]["result_summary"] == "awaiting_user_clarification"
    assert any(event.get("type") == "done" and event.get("content") == "" for event in broadcasts)


@pytest.mark.asyncio
async def test_execute_web_chat_run_stops_after_create_employee_success_card(monkeypatch):
    import json

    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    created_agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    runtime_task = SimpleNamespace(
        id=run_id,
        parent_agent_id=agent_id,
        parent_session_id=session_id,
        prompt="Create the employee",
        metadata_json={"user_id": str(user_id), "session_id": session_id},
        trace_id=None,
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="HR",
        role_description="",
        primary_model_id=uuid4(),
        fallback_model_id=None,
        tenant_id=uuid4(),
        agent_type="native",
    )
    user = SimpleNamespace(id=user_id, display_name="Rocky", username="rocky")
    llm_model = SimpleNamespace(provider="openai", model="gpt-4.1", supports_vision=False)
    broadcasts: list[dict] = []
    finalized_without_assistant: list[dict] = []

    async def fake_load_context(_run_uuid):
        return runtime_task, agent, user, llm_model, None, []

    async def fake_invoke(request):
        await request.on_tool_call(
            {
                "name": "create_digital_employee",
                "args": {"name": "RWA项目与营销专员"},
                "status": "done",
                "result": json.dumps(
                    {
                        "status": "success",
                        "agent_id": str(created_agent_id),
                        "agent_name": "RWA项目与营销专员",
                        "message": "Successfully created digital employee.",
                    }
                ),
            }
        )
        raise AssertionError("kernel must stop after a successful create_digital_employee card")

    async def fail_assistant_finalize(**kwargs):
        raise AssertionError(f"create employee success must not wait for assistant content={kwargs.get('content')!r}")

    async def fake_finalize_without_assistant(**kwargs):
        finalized_without_assistant.append(kwargs)
        return True

    async def fake_broadcast(_agent_id, _session_id, event):
        broadcasts.append(event)

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_load_runtime_context", fake_load_context)
    monkeypatch.setattr(runtime, "_maybe_handle_plan_mode_entry", noop_async)
    monkeypatch.setattr(runtime, "invoke_agent", fake_invoke)
    monkeypatch.setattr(runtime, "_finalize_web_chat_run_with_assistant", fail_assistant_finalize)
    monkeypatch.setattr(
        runtime, "_finalize_web_chat_run_without_assistant", fake_finalize_without_assistant, raising=False
    )
    monkeypatch.setattr(runtime, "_persist_runtime_event", noop_async)
    monkeypatch.setattr(runtime, "_persist_tool_call", noop_async)
    monkeypatch.setattr(runtime, "_update_runtime_task", noop_async)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)
    monkeypatch.setattr(runtime, "_resume_queued_plan_handoffs", noop_async)

    await runtime.execute_web_chat_run(run_id)

    assert len(finalized_without_assistant) == 1
    assert finalized_without_assistant[0]["status"] == "completed"
    assert finalized_without_assistant[0]["result_summary"] == "create_digital_employee_success"
    assert any(event.get("type") == "done" and event.get("content") == "" for event in broadcasts)


@pytest.mark.asyncio
async def test_start_web_chat_run_creates_runtime_task_and_user_message(monkeypatch, tmp_path):
    import app.services.web_chat_runtime as runtime
    from app.memory.t0.ledger import replay_t0_session_events
    from app.models.audit import ChatMessage
    from app.models.runtime_root_item import RuntimeRootItem
    from app.models.runtime_task import RuntimeTask

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, name="Agent", tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, username="rocky", display_name="Rocky")
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=user_id,
        title="Session 05-21",
        last_message_at=None,
    )
    db = _FakeDB(active_run=None)
    scheduled = []
    runtime_lock_order = []

    def fake_create_task(coro):
        scheduled.append(coro)
        coro.close()
        return SimpleNamespace(done=lambda: False, add_done_callback=lambda _cb: None)

    monkeypatch.setattr(runtime.asyncio, "create_task", fake_create_task)

    async def fake_broadcast(*_args, **_kwargs):
        return None

    async def fake_lock_session_runtime_mutation(*_args, **_kwargs):
        runtime_lock_order.append("lock")

    async def fake_find_active_run(*_args, **_kwargs):
        runtime_lock_order.append("find_active")
        return None

    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)
    monkeypatch.setattr(runtime, "_lock_session_runtime_mutation", fake_lock_session_runtime_mutation, raising=False)
    monkeypatch.setattr(runtime, "_find_active_run", fake_find_active_run)
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    result = await runtime.start_web_chat_run(
        db=db,
        agent=agent,
        user=user,
        session=session,
        content="请规划一个长任务",
        display_content="请规划一个长任务",
        file_name="",
    )

    assert result["run_id"]
    assert result["status"] == "pending"
    assert any(isinstance(item, ChatMessage) and item.role == "user" for item in db.added)
    task = next(item for item in db.added if isinstance(item, RuntimeTask))
    root_item = next(item for item in db.added if isinstance(item, RuntimeRootItem))
    assert task.task_type == "web_chat_turn"
    assert task.status == "pending"
    assert task.writer_generation == 1
    assert task.started_at is None
    assert task.parent_agent_id == agent_id
    assert task.child_agent_id == agent_id
    assert task.tenant_id == agent.tenant_id
    assert task.parent_session_id == str(session_id)
    assert task.child_session_id == str(session_id)
    assert task.metadata_json["user_id"] == str(user_id)
    assert task.metadata_json["runtime_task_id"] == task.id.hex
    assert task.metadata_json["request_id"] == str(task.id)
    assert task.metadata_json["trace_id"] == task.trace_id
    assert task.metadata_json["initial_user_message"]["content"] == "请规划一个长任务"
    assert task.metadata_json["initial_user_message"]["message_id"]
    assert task.metadata_json["initial_user_message_t0_materialized"] is False
    assert root_item.runtime_task_id == task.id
    assert root_item.root_runtime_task_id == task.id
    assert root_item.intent_key == f"direct:{task.id}"
    assert root_item.work_type == "direct"
    assert root_item.state == "queued"
    assert root_item.admission_disposition == "admitted"
    assert db.commits == 1
    assert runtime_lock_order[:2] == ["lock", "find_active"]
    assert not scheduled
    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert events == []


@pytest.mark.asyncio
async def test_start_web_chat_run_replays_explicit_run_id_without_duplicate_message(monkeypatch):
    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    session_id = uuid4()
    existing = SimpleNamespace(
        id=run_id,
        task_type="web_chat_turn",
        parent_agent_id=agent_id,
        parent_session_id=str(session_id),
        status="pending",
        created_at=None,
        started_at=None,
        completed_at=None,
        result_summary=None,
        metadata_json={"intent_id": f"goal:{run_id}"},
    )
    db = _FakeDB(active_run=None)

    async def fake_load_run(*_args, **_kwargs):
        return existing

    monkeypatch.setattr(runtime, "_load_web_chat_run_by_id", fake_load_run)

    result = await runtime.start_web_chat_run(
        db=db,
        agent=SimpleNamespace(id=agent_id, name="Agent", tenant_id=uuid4()),
        user=SimpleNamespace(id=uuid4()),
        session=SimpleNamespace(id=session_id, title="Session"),
        content="Do not append this twice",
        run_id=run_id,
    )

    assert result["run_id"] == run_id.hex
    assert result["status"] == "pending"
    assert result["replayed"] is True
    assert db.added == []
    assert db.commits == 0


@pytest.mark.asyncio
async def test_start_web_chat_run_accepts_goal_continuation_task_type(monkeypatch, tmp_path):
    import app.services.web_chat_runtime as runtime
    from app.models.audit import ChatMessage
    from app.models.runtime_task import RuntimeTask

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, name="Agent", tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, username="rocky", display_name="Rocky")
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=user_id,
        title="Session 05-21",
        last_message_at=None,
    )
    db = _FakeDB(active_run=None)
    scheduled = []

    def fake_create_task(coro):
        scheduled.append(coro)
        coro.close()
        return SimpleNamespace(done=lambda: False, add_done_callback=lambda _cb: None)

    monkeypatch.setattr(runtime.asyncio, "create_task", fake_create_task)

    async def fake_broadcast(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    result = await runtime.start_web_chat_run(
        db=db,
        agent=agent,
        user=user,
        session=session,
        content="Continue working toward the active goal.",
        runtime_task_type="goal_continuation",
        append_user_message=False,
        extra_metadata={"goal_id": "goal-1"},
    )

    task = next(item for item in db.added if isinstance(item, RuntimeTask))
    assert result["run_id"] == task.id.hex
    assert task.task_type == "goal_continuation"
    assert task.metadata_json["source"] == "goal_continuation"
    assert task.metadata_json["goal_id"] == "goal-1"
    assert not any(isinstance(item, ChatMessage) for item in db.added)
    assert result["status"] == "pending"
    assert task.status == "pending"
    assert task.writer_generation == 1
    assert not scheduled


@pytest.mark.asyncio
async def test_start_web_chat_run_inherits_existing_budget_run_without_creating_root(monkeypatch):
    import app.services.web_chat_runtime as runtime
    from app.models.runtime_task import RuntimeTask

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    inherited_budget_run_id = uuid4()
    agent = SimpleNamespace(id=agent_id, name="Agent", tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, username="rocky", display_name="Rocky")
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=user_id,
        title="Session 05-21",
        last_message_at=None,
    )
    db = _FakeDB(active_run=None)

    async def fail_create_budget_root(**_kwargs):
        raise AssertionError("continuation with an inherited budget must not create a new budget root")

    async def fake_broadcast(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_create_runtime_budget_root_run_for_chat", fail_create_budget_root)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)

    await runtime.start_web_chat_run(
        db=db,
        agent=agent,
        user=user,
        session=session,
        content="Subagent finished. Continue the parent chain.",
        append_user_message=False,
        extra_metadata={
            "source": "subagent_wake",
            "budget_run_id": str(inherited_budget_run_id),
        },
    )

    task = next(item for item in db.added if isinstance(item, RuntimeTask))
    assert task.budget_run_id == inherited_budget_run_id
    assert task.budget_admission_status == "inherited"
    assert task.metadata_json["budget_run_id"] == str(inherited_budget_run_id)


@pytest.mark.asyncio
async def test_start_web_chat_run_does_not_append_t0_or_dispatch_from_api(monkeypatch):
    import app.services.web_chat_runtime as runtime
    from app.models.runtime_task import RuntimeTask

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, name="Agent", tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, username="rocky", display_name="Rocky")
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=user_id,
        title="Session 05-21",
        last_message_at=None,
    )

    class _FlushTrackingDB(_FakeDB):
        def __init__(self):
            super().__init__(active_run=None)
            self.runtime_task_flushed = False

        async def flush(self):
            self.runtime_task_flushed = any(isinstance(item, RuntimeTask) for item in self.added)

    db = _FlushTrackingDB()
    append_calls = []
    scheduled = []

    async def fake_append_session_event(**kwargs):
        append_calls.append(kwargs)

    def fake_create_task(coro):
        scheduled.append(coro)
        coro.close()
        return SimpleNamespace(done=lambda: False, add_done_callback=lambda _cb: None)

    async def fake_broadcast(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "append_session_event", fake_append_session_event)
    monkeypatch.setattr(runtime.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)

    await runtime.start_web_chat_run(
        db=db,
        agent=agent,
        user=user,
        session=session,
        content="hello",
    )

    assert db.runtime_task_flushed is True
    assert append_calls == []
    assert scheduled == []


@pytest.mark.asyncio
async def test_worker_materializes_initial_user_turn_to_transcript_without_duplicate_chat_message(
    monkeypatch, tmp_path
):
    import app.services.web_chat_runtime as runtime
    from app.models.audit import ChatMessage
    from app.models.chat_transcript_event import ChatTranscriptEvent

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    run_id = uuid4()
    message_id = uuid4()
    agent = SimpleNamespace(id=agent_id, name="Agent", tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, username="rocky", display_name="Rocky")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id)
    runtime_task = SimpleNamespace(
        id=run_id,
        parent_session_id=str(session_id),
        metadata_json={
            "source": "web",
            "initial_user_message_t0_materialized": False,
            "initial_user_message": {
                "message_id": str(message_id),
                "content": "请规划一个长任务",
                "llm_content": "请规划一个长任务",
                "display_content": "请规划一个长任务",
                "file_name": "",
                "source": "web",
                "attachments": [],
                "parts": [],
                "metadata": {"turn_id": "turn-1", "intent_id": "intent-1"},
            },
        },
    )
    db = _FakeDB(active_run=None)
    snapshots = []
    answered = []

    async def fake_capture(**kwargs):
        snapshots.append(kwargs)

    async def fake_mark(**kwargs):
        answered.append(kwargs)
        answer_event = kwargs["answer_event"]
        answer_metadata = dict(answer_event.transcript_event.metadata_json or {})
        answer_metadata["elicitation_effective_answer"] = "请规划整个公司的长任务"
        answer_event.transcript_event.metadata_json = answer_metadata

    monkeypatch.setattr(runtime, "_capture_user_checkpoint_workspace_snapshot", fake_capture)
    monkeypatch.setattr(runtime, "mark_latest_pending_clarification_answered", fake_mark)
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    await runtime._materialize_initial_user_turn_for_worker(
        db=db,
        runtime_task=runtime_task,
        agent=agent,
        user=user,
        session=session,
    )

    assert runtime_task.metadata_json["initial_user_message_t0_materialized"] is True
    assert runtime_task.metadata_json["initial_user_message_t0_event_id"]
    assert runtime_task.prompt == "请规划整个公司的长任务"
    assert runtime_task.metadata_json["elicitation_original_prompt"] == "请规划一个长任务"
    assert runtime_task.metadata_json["elicitation_effective_prompt"] == "请规划整个公司的长任务"
    assert snapshots
    assert answered
    assert not any(isinstance(item, ChatMessage) for item in db.added)
    events = [item for item in db.added if isinstance(item, ChatTranscriptEvent)]
    assert [(event.event_type, event.content) for event in events] == [("user_message", "请规划一个长任务")]
    assert events[0].message_id == message_id
    assert events[0].metadata_json["worker_materialized"] is True
    assert events[0].projection_status == "pending"


@pytest.mark.asyncio
async def test_start_channel_chat_run_from_saved_turn_creates_runtime_task_without_duplicate_user_message(
    monkeypatch, tmp_path
):
    import app.services.web_chat_runtime as runtime
    from app.memory.t0.ledger import replay_t0_session_events
    from app.models.audit import ChatMessage
    from app.models.runtime_task import RuntimeTask

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, name="Agent", tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, username="feishu_u", display_name="Feishu User")
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=user_id,
        title="Feishu Session",
        last_message_at=None,
        delivery_target_json={"channel": "feishu", "receive_id": "ou_1"},
        transcript_metadata_json={
            "permission_mode": "bypassPermissions",
            "session_permission_allowed_tools": ["web_search"],
        },
    )
    db = _FakeDB(active_run=None)
    scheduled = []
    runtime_lock_order = []

    def fake_create_task(coro):
        scheduled.append(coro)
        coro.close()
        return SimpleNamespace(done=lambda: False, add_done_callback=lambda _cb: None)

    monkeypatch.setattr(runtime.asyncio, "create_task", fake_create_task)

    async def fake_broadcast(*_args, **_kwargs):
        return None

    async def fake_lock_session_runtime_mutation(*_args, **_kwargs):
        runtime_lock_order.append("lock")

    async def fake_find_active_run(*_args, **_kwargs):
        runtime_lock_order.append("find_active")
        return None

    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)
    monkeypatch.setattr(runtime, "_lock_session_runtime_mutation", fake_lock_session_runtime_mutation, raising=False)
    monkeypatch.setattr(runtime, "_find_active_run", fake_find_active_run)
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    result = await runtime.start_channel_chat_run_from_saved_turn(
        db=db,
        agent=agent,
        user=user,
        session=session,
        content="处理这条飞书消息",
        source_channel="feishu",
    )

    assert result["run_id"]
    assert result["status"] == "pending"
    assert not any(isinstance(item, ChatMessage) for item in db.added)
    task = next(item for item in db.added if isinstance(item, RuntimeTask))
    assert task.task_type == "web_chat_turn"
    assert task.status == "pending"
    assert task.writer_generation == 1
    assert task.started_at is None
    assert task.tenant_id == agent.tenant_id
    assert task.parent_session_id == str(session_id)
    assert task.prompt == "处理这条飞书消息"
    assert task.metadata_json["runtime_task_id"] == task.id.hex
    assert task.metadata_json["request_id"] == str(task.id)
    assert task.metadata_json["trace_id"] == task.trace_id
    assert task.metadata_json["source"] == "feishu"
    assert task.metadata_json["channel"] == "feishu"
    assert task.metadata_json["delivery_target_json"] == session.delivery_target_json
    assert task.metadata_json["permission_mode"] == "bypassPermissions"
    assert task.metadata_json["writable_roots"] == ["workspace/"]
    assert task.metadata_json["permission_profile"] == {
        "mode": "bypassPermissions",
        "allowed_tools": ["web_search"],
        "writable_roots": ["workspace/"],
        "session_grants": [],
    }
    assert task.metadata_json["initial_user_message"]["content"] == "处理这条飞书消息"
    assert task.metadata_json["initial_user_message"]["source"] == "feishu"
    assert task.metadata_json["initial_user_message_t0_materialized"] is False
    assert runtime_lock_order[:2] == ["lock", "find_active"]
    assert not scheduled
    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert events == []


@pytest.mark.asyncio
async def test_channel_ingress_replay_reuses_the_exact_runtime_task(monkeypatch):
    import app.services.web_chat_runtime as runtime
    from app.models.runtime_task import RuntimeTask
    from app.services.channel_ingress_context import use_channel_ingress_context

    event_id = uuid4()
    tenant_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    existing = RuntimeTask(
        id=uuid4(),
        task_type="web_chat_turn",
        status="completed",
        parent_agent_id=agent_id,
        child_agent_id=agent_id,
        parent_session_id=str(session_id),
        child_session_id=str(session_id),
        tenant_id=tenant_id,
        root_idempotency_key=f"channel-ingress:{event_id}",
        metadata_json={"channel_ingress_event_id": str(event_id)},
    )
    agent = SimpleNamespace(id=agent_id, name="Agent", tenant_id=tenant_id)
    user = SimpleNamespace(id=user_id, username="slack_u", display_name="Slack User")
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=user_id,
        title="Slack Session",
        last_message_at=None,
        delivery_target_json={"channel": "slack", "channel_id": "C1"},
        transcript_metadata_json={},
    )
    db = _FakeDB(active_run=existing)

    async def fake_broadcast(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)

    with use_channel_ingress_context(
        event_id=event_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
    ) as ingress:
        result = await runtime.start_channel_chat_run_from_saved_turn(
            db=db,
            agent=agent,
            user=user,
            session=session,
            content="do not duplicate me",
            source_channel="slack",
        )

    assert result["run_id"] == existing.id.hex
    assert result["status"] == "completed"
    assert "queued_user_message" not in result
    assert db.added == []
    assert ingress.runtime_task_id == existing.id
    assert ingress.session_id == session_id


@pytest.mark.asyncio
async def test_channel_ingress_active_run_uses_stable_canonical_input_identity(monkeypatch):
    import app.services.web_chat_runtime as runtime
    from app.models.runtime_task import RuntimeTask
    from app.services.channel_ingress_context import use_channel_ingress_context

    event_id = uuid4()
    tenant_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    active = RuntimeTask(
        id=uuid4(),
        task_type="web_chat_turn",
        status="running",
        parent_agent_id=agent_id,
        child_agent_id=agent_id,
        parent_session_id=str(session_id),
        child_session_id=str(session_id),
        tenant_id=tenant_id,
        metadata_json={"turn_id": "turn-active"},
    )

    class _SequenceDB(_FakeDB):
        def __init__(self):
            super().__init__()
            self.results = [None, active, None]

        async def execute(self, _stmt):
            return _ScalarResult(self.results.pop(0))

    db = _SequenceDB()
    agent = SimpleNamespace(id=agent_id, name="Agent", tenant_id=tenant_id)
    user = SimpleNamespace(id=user_id, username="slack_u", display_name="Slack User")
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=user_id,
        title="Slack Session",
        last_message_at=None,
        delivery_target_json={"channel": "slack", "channel_id": "C1"},
        transcript_metadata_json={},
    )

    async def fake_lock(*_args, **_kwargs):
        return None

    async def fake_broadcast(*_args, **_kwargs):
        return None

    captured = {}

    async def fake_canonical(**kwargs):
        captured.update(kwargs)
        return await _fake_canonical_active_input(**kwargs)

    monkeypatch.setattr(runtime, "_lock_session_runtime_mutation", fake_lock)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)
    monkeypatch.setattr(runtime, "_submit_active_session_input", fake_canonical)

    with use_channel_ingress_context(
        event_id=event_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
    ) as ingress:
        result = await runtime.start_channel_chat_run_from_saved_turn(
            db=db,
            agent=agent,
            user=user,
            session=session,
            content="queued once",
            source_channel="slack",
        )

    assert captured["idempotency_key"] == f"channel-ingress:{event_id}"
    assert "pending_user_messages" not in active.metadata_json
    assert result["queued_user_message"]["status"] == "queued"
    assert ingress.runtime_task_id == active.id


@pytest.mark.asyncio
async def test_start_channel_chat_run_from_saved_turn_does_not_append_t0_or_dispatch_from_api(monkeypatch):
    import app.services.web_chat_runtime as runtime
    from app.models.runtime_task import RuntimeTask

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, name="Agent", tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, username="feishu_u", display_name="Feishu User")
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=user_id,
        title="Feishu Session",
        last_message_at=None,
        delivery_target_json={"channel": "feishu", "receive_id": "ou_1"},
    )

    class _FlushTrackingDB(_FakeDB):
        def __init__(self):
            super().__init__(active_run=None)
            self.runtime_task_flushed = False

        async def flush(self):
            self.runtime_task_flushed = any(isinstance(item, RuntimeTask) for item in self.added)

    db = _FlushTrackingDB()
    append_calls = []
    scheduled = []

    async def fake_append_session_event(**kwargs):
        append_calls.append(kwargs)

    def fake_create_task(coro):
        scheduled.append(coro)
        coro.close()
        return SimpleNamespace(done=lambda: False, add_done_callback=lambda _cb: None)

    async def fake_broadcast(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "append_session_event", fake_append_session_event)
    monkeypatch.setattr(runtime.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)

    await runtime.start_channel_chat_run_from_saved_turn(
        db=db,
        agent=agent,
        user=user,
        session=session,
        content="处理这条飞书消息",
        source_channel="feishu",
    )

    assert db.runtime_task_flushed is True
    assert append_calls == []
    assert scheduled == []


@pytest.mark.asyncio
async def test_start_web_chat_run_queues_user_message_when_run_is_active(monkeypatch, tmp_path):
    import app.services.web_chat_runtime as runtime
    from app.memory.t0.ledger import replay_t0_session_events
    from app.models.audit import ChatMessage

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    existing_run_id = uuid4()
    agent = SimpleNamespace(id=agent_id, name="Agent", tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, username="rocky", display_name="Rocky")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id)
    active_run = SimpleNamespace(
        id=existing_run_id,
        status="running",
        created_at=None,
        started_at=None,
        completed_at=None,
        result_summary=None,
        metadata_json={},
    )
    db = _FakeDB(active_run=active_run)
    monkeypatch.setattr(runtime, "_submit_active_session_input", _fake_canonical_active_input)
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    with pytest.raises(runtime.ActiveWebChatRunExists) as exc_info:
        await runtime.start_web_chat_run(
            db=db,
            agent=agent,
            user=user,
            session=session,
            content="second message",
        )

    assert exc_info.value.run["run_id"] == existing_run_id.hex
    assert exc_info.value.run["status"] == "running"
    assert exc_info.value.run["queued_user_message"]["content"] == "second message"
    assert not any(isinstance(item, ChatMessage) for item in db.added)
    assert "pending_user_messages" not in active_run.metadata_json
    assert db.commits == 0
    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert events == []


@pytest.mark.asyncio
async def test_start_web_chat_run_preserves_structured_mid_run_attachment_queue(monkeypatch, tmp_path):
    import app.services.web_chat_runtime as runtime
    from app.models.audit import ChatMessage

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    existing_run_id = uuid4()
    agent = SimpleNamespace(id=agent_id, name="Agent", tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, username="rocky", display_name="Rocky")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id)
    active_run = SimpleNamespace(
        id=existing_run_id,
        status="running",
        created_at=None,
        started_at=None,
        completed_at=None,
        result_summary=None,
        metadata_json={},
    )
    db = _FakeDB(active_run=active_run)
    monkeypatch.setattr(runtime, "_submit_active_session_input", _fake_canonical_active_input)
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    with pytest.raises(runtime.ActiveWebChatRunExists) as exc_info:
        await runtime.start_web_chat_run(
            db=db,
            agent=agent,
            user=user,
            session=session,
            content="[File: bank.pdf]\nFull extracted text\n\nQuestion: summarize",
            display_content="[file:bank.pdf]\nsummarize",
            file_name="bank.pdf",
            attachments=[{"name": "bank.pdf", "path": "workspace/bank.pdf"}],
        )

    queued = exc_info.value.run["queued_user_message"]
    assert queued["content"] == "[file:bank.pdf]\n[file:bank.pdf]\nsummarize"
    assert queued["llm_content"] == "[File: bank.pdf]\nFull extracted text\n\nQuestion: summarize"
    assert queued["display_content"] == "[file:bank.pdf]\nsummarize"
    assert queued["attachments"] == [{"name": "bank.pdf", "path": "workspace/bank.pdf"}]
    assert not any(isinstance(item, ChatMessage) for item in db.added)
    assert "pending_user_messages" not in active_run.metadata_json


@pytest.mark.asyncio
async def test_steer_active_web_chat_turn_queues_message_for_matching_turn(monkeypatch, tmp_path):
    import app.services.web_chat_runtime as runtime
    from app.memory.t0.ledger import replay_t0_session_events
    from app.models.audit import ChatMessage

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    existing_run_id = uuid4()
    agent = SimpleNamespace(id=agent_id, name="Agent", tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, username="rocky", display_name="Rocky")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id)
    active_run = SimpleNamespace(
        id=existing_run_id,
        status="running",
        created_at=None,
        started_at=None,
        completed_at=None,
        result_summary=None,
        metadata_json={"turn_id": "turn-1"},
    )
    db = _FakeDB(active_run=active_run)
    monkeypatch.setattr(runtime, "_submit_active_session_input", _fake_canonical_active_input)
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    result = await runtime.steer_active_web_chat_turn(
        db=db,
        agent=agent,
        user=user,
        session=session,
        content="Use the stricter interpretation.",
        expected_turn_id="turn-1",
    )

    assert result["run_id"] == existing_run_id.hex
    assert result["turn_id"] == "turn-1"
    assert result["queued"]["content"] == "Use the stricter interpretation."
    assert result["steer_strategy"] == "canonical_session_v2_input"
    assert not any(isinstance(item, ChatMessage) for item in db.added)
    assert "pending_user_messages" not in active_run.metadata_json
    assert db.commits == 0
    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert events == []


@pytest.mark.asyncio
async def test_steer_active_web_chat_turn_rejects_stale_turn_id():
    from fastapi import HTTPException

    import app.services.web_chat_runtime as runtime

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, name="Agent", tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, username="rocky", display_name="Rocky")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id)
    active_run = SimpleNamespace(
        id=uuid4(),
        status="running",
        created_at=None,
        started_at=None,
        completed_at=None,
        result_summary=None,
        metadata_json={"turn_id": "turn-current"},
    )
    db = _FakeDB(active_run=active_run)

    with pytest.raises(HTTPException) as exc:
        await runtime.steer_active_web_chat_turn(
            db=db,
            agent=agent,
            user=user,
            session=session,
            content="This belongs to an old turn.",
            expected_turn_id="turn-old",
        )

    assert exc.value.status_code == 409
    assert "active turn has changed" in exc.value.detail


@pytest.mark.asyncio
async def test_persist_legacy_tool_call_appends_typed_transcript_tool_result(monkeypatch, tmp_path):
    import app.services.tenant_resolver as tenant_resolver
    import app.services.web_chat_runtime as runtime
    from app.models.chat_transcript_event import ChatTranscriptEvent

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    added = []

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def add(self, value):
            added.append(value)

        async def commit(self):
            return None

    async def fake_resolve_tenant_for_agent(_agent_id):
        return tenant_id

    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda _tenant_id: _Session())
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    full_result = "file content\n" + ("R" * 60000) + "\nEND_OF_PERSISTED_TOOL_RESULT"
    await runtime._persist_legacy_tool_call(
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        data={"name": "read_file", "args": {"path": "workspace/a.md"}, "result": full_result},
    )

    assert added[0].role == "tool_call"
    events = [item for item in added if isinstance(item, ChatTranscriptEvent)]
    assert [(event.event_type, event.item_type) for event in events] == [("tool_result", "tool_result")]
    assert events[0].metadata_json["tool_name"] == "read_file"
    assert events[0].metadata_json["status"] == "done"
    assert events[0].projection_status == "pending"
    assert json.loads(events[0].content)["result"] == full_result


@pytest.mark.asyncio
async def test_persist_personal_kb_tool_keeps_full_evidence_but_replays_pointer(monkeypatch, tmp_path):
    import app.services.tenant_resolver as tenant_resolver
    import app.services.web_chat_runtime as runtime
    from app.models.chat_transcript_event import ChatTranscriptEvent

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    document_id = str(uuid4())
    segment_id = str(uuid4())
    source_ref = f"kb://person/{user_id}/documents/{document_id}#segment={segment_id}"
    raw_result = json.dumps(
        {
            "results": [
                {
                    "document_id": document_id,
                    "segment_id": segment_id,
                    "title": "PRIVATE-TITLE",
                    "snippet": "PRIVATE-SNIPPET",
                    "source_ref": source_ref,
                    "score_trace": {"secret": "PRIVATE-TRACE"},
                }
            ],
            "warnings": [],
        }
    )
    added = []

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def add(self, value):
            added.append(value)

        async def commit(self):
            return None

    async def fake_resolve_tenant_for_agent(_agent_id):
        return tenant_id

    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda _tenant_id: _Session())
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    await runtime._persist_legacy_tool_call(
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        data={
            "name": "search_personal_kb",
            "args": {"query": "operating notes", "limit": 5},
            "status": "done",
            "tool_call_id": "call_personal_kb",
            "result": raw_result,
            "content_replacement": {
                "schema": "content_replacement_record.v1",
                "tool_name": "search_personal_kb",
                "tool_call_id": "call_personal_kb",
                "reason": "result size threshold",
                "inline_content": raw_result,
            },
        },
    )

    events = [item for item in added if isinstance(item, ChatTranscriptEvent)]
    assert len(events) == 1
    assert "PRIVATE-TITLE" in events[0].content
    assert "PRIVATE-SNIPPET" in events[0].content

    persisted_payload = json.loads(events[0].content)
    replay_pointer = persisted_payload["content_replacement"]["inline_content"]
    assert json.loads(replay_pointer)["schema"] == "knowledge_tool_replay.v1"
    assert "PRIVATE-TITLE" not in replay_pointer
    assert "PRIVATE-SNIPPET" not in replay_pointer
    assert "PRIVATE-TRACE" not in replay_pointer

    conversation = runtime.conversation_from_history_messages(
        [SimpleNamespace(role="tool_call", id="db-personal-kb", content=events[0].content)]
    )
    assert conversation[0]["tool_calls"][0]["id"] == "call_personal_kb"
    assert conversation[1]["tool_call_id"] == "call_personal_kb"
    assert conversation[1]["content"] == replay_pointer


@pytest.mark.asyncio
async def test_persist_legacy_tool_call_attaches_written_artifact_parts(monkeypatch, tmp_path):
    import app.services.tenant_resolver as tenant_resolver
    import app.services.web_chat_runtime as runtime
    from app.models.chat_artifact import ChatArtifact
    from app.models.chat_transcript_event import ChatTranscriptEvent

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    run_id = uuid4()
    artifact_file = tmp_path / str(agent_id) / "workspace" / "proposal.docx"
    artifact_file.parent.mkdir(parents=True)
    artifact_file.write_bytes(b"docx")
    added = []

    class _Session:
        def __init__(self):
            self.existing_artifact = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: self.existing_artifact)

        def add(self, value):
            added.append(value)
            if isinstance(value, ChatArtifact):
                self.existing_artifact = value

        async def flush(self):
            return None

        async def commit(self):
            return None

    async def fake_resolve_tenant_for_agent(_agent_id):
        return tenant_id

    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda _tenant_id: _Session())
    monkeypatch.setattr(runtime, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    await runtime._persist_legacy_tool_call(
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        data={
            "name": "office_document_apply",
            "args": {
                "path": "workspace/proposal.docx",
                "operations": [{"op": "replace_text", "text": "new"}],
            },
            "status": "done",
            "result": '{"ok": true}',
            "runtime_task_id": str(run_id),
        },
    )

    artifacts = [item for item in added if isinstance(item, ChatArtifact)]
    transcript_events = [item for item in added if isinstance(item, ChatTranscriptEvent)]
    tool_event = next(event for event in transcript_events if event.event_type == "tool_result")

    assert len(artifacts) == 1
    assert artifacts[0].path == "workspace/proposal.docx"
    assert tool_event.parts_json
    assert tool_event.parts_json[0]["type"] == "artifact"
    assert tool_event.parts_json[0]["path"] == "workspace/proposal.docx"
    assert tool_event.metadata_json["artifact_ids"] == [str(artifacts[0].id)]


@pytest.mark.asyncio
async def test_persist_legacy_tool_call_appends_running_step_contract(monkeypatch, tmp_path):
    import app.services.tenant_resolver as tenant_resolver
    import app.services.web_chat_runtime as runtime
    from app.models.chat_transcript_event import ChatTranscriptEvent

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    run_id = uuid4()
    added = []

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def add(self, value):
            added.append(value)

        async def commit(self):
            return None

    async def fake_resolve_tenant_for_agent(_agent_id):
        return tenant_id

    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda _tenant_id: _Session())
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    persisted = await runtime._persist_legacy_tool_call(
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        data={
            "id": "toolu_123",
            "name": "read_file",
            "args": {"path": "workspace/a.md"},
            "status": "running",
            "runtime_task_id": str(run_id),
            "started_at": "2026-06-22T10:00:00Z",
        },
    )

    assert persisted is not None
    assert added[0].role == "tool_call"
    events = [item for item in added if isinstance(item, ChatTranscriptEvent)]
    assert [(event.event_type, event.item_type) for event in events] == [("tool_call", "tool_call")]
    assert events[0].run_id == run_id
    assert events[0].metadata_json["tool_name"] == "read_file"
    assert events[0].metadata_json["status"] == "running"
    assert events[0].metadata_json["tool_call_id"] == "toolu_123"
    assert events[0].metadata_json["step_id"] == "tool:toolu_123"
    assert events[0].metadata_json["visibility"] == "collapsed"
    assert '"status": "running"' in events[0].content
    assert '"tool_call_id": "toolu_123"' in events[0].content
    assert '"step_id": "tool:toolu_123"' in events[0].content
    assert '"duration_ms"' not in events[0].content


@pytest.mark.asyncio
async def test_persist_legacy_tool_call_done_contract_includes_stable_ids_and_duration(monkeypatch, tmp_path):
    import app.services.tenant_resolver as tenant_resolver
    import app.services.web_chat_runtime as runtime
    from app.models.chat_transcript_event import ChatTranscriptEvent

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    run_id = uuid4()
    added = []

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def add(self, value):
            added.append(value)

        async def commit(self):
            return None

    async def fake_resolve_tenant_for_agent(_agent_id):
        return tenant_id

    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda _tenant_id: _Session())
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    await runtime._persist_legacy_tool_call(
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        data={
            "id": "toolu_123",
            "name": "read_file",
            "args": {"path": "workspace/a.md"},
            "status": "done",
            "result": "file content",
            "runtime_task_id": str(run_id),
            "started_at": "2026-06-22T10:00:00Z",
            "completed_at": "2026-06-22T10:00:02.500Z",
        },
    )

    events = [item for item in added if isinstance(item, ChatTranscriptEvent)]
    assert [(event.event_type, event.item_type) for event in events] == [("tool_result", "tool_result")]
    assert events[0].metadata_json["tool_call_id"] == "toolu_123"
    assert events[0].metadata_json["step_id"] == "tool:toolu_123"
    assert events[0].metadata_json["duration_ms"] == 2500
    assert events[0].metadata_json["visibility"] == "collapsed"
    assert '"duration_ms": 2500' in events[0].content
    assert '"result": "file content"' in events[0].content


@pytest.mark.asyncio
async def test_start_web_chat_run_queues_when_active_run_unique_index_conflicts(monkeypatch, tmp_path):
    import app.services.web_chat_runtime as runtime
    from app.memory.t0.ledger import replay_t0_session_events
    from app.models.audit import ChatMessage
    from app.models.runtime_task import RuntimeTask
    from sqlalchemy.exc import IntegrityError

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    existing_run_id = uuid4()
    agent = SimpleNamespace(id=agent_id, name="Agent", tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, username="rocky", display_name="Rocky")
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=user_id,
        title="Session 06-12",
        last_message_at=None,
    )
    active_run = SimpleNamespace(
        id=existing_run_id,
        status="running",
        created_at=None,
        started_at=None,
        completed_at=None,
        result_summary=None,
        metadata_json={},
    )

    class _Orig:
        diag = SimpleNamespace(constraint_name="uq_runtime_tasks_active_web_chat_session")

    class _ConflictThenActiveDB(_FakeDB):
        def __init__(self):
            super().__init__(active_run=None)
            self.execute_calls = 0
            self.rollbacks = 0

        async def execute(self, _stmt):
            if "session_writer_epochs" in str(_stmt):
                return await super().execute(_stmt)
            if "runtime_root_items" in str(_stmt):
                return _ScalarResult(None)
            self.execute_calls += 1
            return _ScalarResult(None if self.execute_calls == 1 else active_run)

        async def commit(self):
            self.commits += 1
            if self.commits == 1:
                raise IntegrityError("insert runtime_tasks", {}, _Orig())

        async def rollback(self):
            self.rollbacks += 1
            self.added.clear()

    db = _ConflictThenActiveDB()
    broadcasts = []

    async def fake_broadcast(_agent_id, _session_id, event):
        broadcasts.append(event)

    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)
    monkeypatch.setattr(runtime, "_submit_active_session_input", _fake_canonical_active_input)
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    with pytest.raises(runtime.ActiveWebChatRunExists) as exc_info:
        await runtime.start_web_chat_run(
            db=db,
            agent=agent,
            user=user,
            session=session,
            content="race message",
        )

    assert exc_info.value.run["run_id"] == existing_run_id.hex
    assert exc_info.value.run["status"] == "running"
    assert exc_info.value.run["queued_user_message"]["content"] == "race message"
    assert db.rollbacks == 1
    assert db.commits == 1
    assert not any(isinstance(item, ChatMessage) for item in db.added)
    assert not any(isinstance(item, RuntimeTask) for item in db.added)
    assert "pending_user_messages" not in active_run.metadata_json
    assert broadcasts[-1]["type"] == "user_message_queued"
    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert events == []


@pytest.mark.asyncio
async def test_resume_queued_plan_handoffs_restarts_oldest_confirmed_plan(monkeypatch):
    """A queued Plan Mode handoff must be resumable after the current run exits.

    Returning ``handoff_status='queued'`` from the handoff handler is only honest
    if the web-chat runtime has a recovery hook that calls the handoff again once
    the active run is no longer active.
    """
    import app.services.web_chat_runtime as runtime
    from sqlalchemy.dialects import postgresql

    agent_id = uuid4()
    session_id = "sess-1"
    plan_id = uuid4()
    active_run_id = uuid4().hex

    class _QueuedResult:
        def scalars(self):
            return SimpleNamespace(all=lambda: [plan_id])

    class _QueuedDB:
        async def execute(self, _stmt):
            compiled = _stmt.compile(dialect=postgresql.dialect())
            assert "handoff_payload" in str(compiled)
            assert "active_run_id" in {str(value) for value in compiled.params.values()}
            assert active_run_id in {str(value) for value in compiled.params.values()}
            return _QueuedResult()

    class _SessionFactory:
        async def __aenter__(self):
            return _QueuedDB()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    calls = []

    class _Service:
        async def handoff_confirmed_plan(self, *, plan_id):
            calls.append(plan_id)
            return SimpleNamespace(id=plan_id, handoff_status="completed")

    # RLS stage-2a: the scan now runs under tenant_scoped_session after resolving
    # the agent's tenant (audited bypass). Route both to fakes — the assertions on
    # the queued-handoff statement are unchanged.
    async def _fake_resolve_tenant(*_a, **_k):
        return uuid4()

    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda *a, **k: _SessionFactory())
    monkeypatch.setattr("app.services.tenant_resolver.resolve_tenant_for_agent", _fake_resolve_tenant)
    monkeypatch.setattr("app.services.plan_mode_service.get_plan_mode_service", lambda: _Service())

    resumed = await runtime._resume_queued_plan_handoffs(
        agent_id=agent_id,
        session_id=session_id,
        completed_run_id=active_run_id,
    )

    assert calls == [plan_id]
    assert resumed == [str(plan_id)]


@pytest.mark.asyncio
async def test_execute_web_chat_run_resumes_queued_plan_handoffs_on_terminal_exit(monkeypatch):
    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    session_id = "sess-1"
    runtime_task = SimpleNamespace(
        id=run_id,
        parent_agent_id=agent_id,
        parent_session_id=session_id,
        prompt="hello",
        metadata_json={"user_id": str(user_id), "session_id": session_id},
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="Agent",
        role_description="",
        primary_model_id=None,
        fallback_model_id=None,
        tenant_id=uuid4(),
        agent_type="standard",
    )
    user = SimpleNamespace(id=user_id)
    resumed = []

    async def fake_load_context(_run_uuid):
        return runtime_task, agent, user, None, None, []

    async def fake_resume(**kwargs):
        resumed.append(kwargs)
        return ["plan-1"]

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_load_runtime_context", fake_load_context)
    monkeypatch.setattr(runtime, "_maybe_handle_plan_mode_entry", noop_async)
    monkeypatch.setattr(runtime, "_persist_assistant_message", noop_async)
    monkeypatch.setattr(runtime, "_update_runtime_task", noop_async)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", noop_async)
    monkeypatch.setattr(runtime, "_resume_queued_plan_handoffs", fake_resume)

    await runtime.execute_web_chat_run(run_id)

    assert resumed == [{"agent_id": agent_id, "session_id": session_id, "completed_run_id": run_id.hex}]


@pytest.mark.asyncio
async def test_resume_persisted_web_chat_runs_only_wakes_the_fenced_worker(monkeypatch):
    import app.services.web_chat_runtime as runtime
    from app.services import runtime_task_worker

    run_id = uuid4()
    agent_id = uuid4()
    tenant_id = uuid4()
    task = SimpleNamespace(
        id=run_id,
        tenant_id=tenant_id,
        task_type="web_chat_turn",
        status="running",
        parent_agent_id=agent_id,
        metadata_json={},
    )

    class _Rows:
        def scalars(self):
            return SimpleNamespace(all=lambda: [task])

    class _DB:
        def __init__(self):
            self.calls = 0
            self.commits = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def execute(self, _stmt):
            self.calls += 1
            return _Rows() if self.calls == 1 else _ScalarResult(None)

        async def commit(self):
            self.commits += 1

    wakeups: list[dict] = []

    async def fake_notify(**kwargs):
        wakeups.append(kwargs)

    def fail_direct_dispatch(*_args, **_kwargs):
        raise AssertionError("startup recovery must not dispatch a RuntimeTask outside the claim worker")

    db = _DB()

    async def fake_list_active(**_kwargs):
        return [{"task_id": run_id.hex, "tenant_id": str(tenant_id)}]

    monkeypatch.setattr(runtime, "list_active_runtime_task_records", fake_list_active)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda *a, **k: db)
    monkeypatch.setattr(runtime, "dispatch_web_chat_run", fail_direct_dispatch)
    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", fake_notify)

    resumed = await runtime.resume_persisted_web_chat_runs(limit=10)

    assert resumed == [run_id.hex]
    assert wakeups == [{"reason": "startup_web_chat_recovery"}]


def test_reclaimed_web_chat_claim_builds_durable_resume_context(monkeypatch):
    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    task = SimpleNamespace(
        id=run_id,
        parent_agent_id=agent_id,
        metadata_json={"reclaimed_expired_claim": True},
    )
    monkeypatch.setattr(
        runtime,
        "build_long_task_resume_context",
        lambda **kwargs: {"resume_prompt": "resume once", "task": kwargs["runtime_task_id"].hex},
    )

    metadata = runtime._with_reclaimed_web_chat_resume_context(task)

    assert metadata["resumed_after_restart"] is True
    assert metadata["recovery_state"] == "recovering"
    assert metadata["restart_resume_context"] == {"resume_prompt": "resume once", "task": run_id.hex}


def test_tool_settlement_uses_runtime_effective_arguments_for_decision_hash() -> None:
    from app.services.web_chat_runtime import _tool_settlement_arguments

    provider_args = {"agent_name": "Web3", "message": "research"}
    effective_args = {
        **provider_args,
        "parent_session_id": "session-1",
        "_requester_user_id": "user-1",
    }

    assert (
        _tool_settlement_arguments(
            {
                "args": provider_args,
                "tool_execution_evidence": {"effective_arguments": effective_args},
            }
        )
        == effective_args
    )


@pytest.mark.asyncio
async def test_claimed_web_chat_run_quarantines_after_recovery_budget_is_exhausted(monkeypatch):
    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    task = SimpleNamespace(
        id=run_id,
        tenant_id=tenant_id,
        task_type="web_chat_turn",
        status="running",
        parent_agent_id=agent_id,
        parent_session_id=str(session_id),
        attempt_count=4,
        claimed_by="recovery-worker",
        claim_expires_at=datetime.now(timezone.utc),
        created_at=None,
        started_at=None,
        completed_at=None,
        result_summary=None,
        metadata_json={"reclaimed_expired_claim": True, "lease_reclaim_count": 3},
    )

    class _DB:
        def __init__(self):
            self.calls = 0
            self.commits = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _stmt):
            self.calls += 1
            return _ScalarResult(task if self.calls == 1 else None)

        async def commit(self):
            self.commits += 1

    persisted_events: list[dict] = []
    broadcasts: list[dict] = []
    db = _DB()

    async def fake_resolve(*_args, **_kwargs):
        return tenant_id

    async def fake_append_session_event(**kwargs):
        persisted_events.append(kwargs)

    async def fake_broadcast(_agent_id, _session_id, event):
        broadcasts.append(event)

    monkeypatch.setattr(runtime, "resolve_tenant_for_runtime_task", fake_resolve)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda *a, **k: db)
    monkeypatch.setattr(runtime, "append_session_event", fake_append_session_event)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)
    monkeypatch.setattr(
        runtime,
        "get_settings",
        lambda: SimpleNamespace(RUNTIME_TASK_WEB_CHAT_MAX_EXECUTION_ATTEMPTS=3),
    )

    stopped = await runtime._reconcile_claimed_web_chat_terminal_ghost(run_id)

    assert stopped is True
    assert task.status == "needs_reconciliation"
    assert task.completed_at is not None
    assert task.claimed_by is None
    assert task.claim_expires_at is None
    assert task.metadata_json["automatic_retry_allowed"] is False
    assert task.metadata_json["reconciliation_reason"] == "web_chat_recovery_attempts_exhausted"
    assert persisted_events[0]["event_type"] == "error"
    assert persisted_events[0]["metadata"]["status"] == "needs_reconciliation"
    assert db.commits == 1
    assert [event["type"] for event in broadcasts] == ["error", "phase"]


@pytest.mark.asyncio
async def test_get_active_web_chat_run_reconciles_terminal_transcript_ghost():
    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    task = SimpleNamespace(
        id=run_id,
        tenant_id=tenant_id,
        task_type="web_chat_turn",
        status="running",
        parent_agent_id=agent_id,
        parent_session_id=str(session_id),
        created_at=None,
        started_at=None,
        completed_at=None,
        result_summary=None,
        metadata_json={},
    )
    full_answer = "final answer\n" + ("A" * 1000) + "\nEND_OF_FINAL_ANSWER"
    terminal_event = SimpleNamespace(
        event_type="assistant_message",
        content=full_answer,
        metadata_json={"status": "completed"},
    )

    class _DB:
        def __init__(self):
            self.calls = 0
            self.commits = 0

        async def execute(self, _stmt):
            self.calls += 1
            return _ScalarResult(task if self.calls == 1 else terminal_event)

        async def commit(self):
            self.commits += 1

    db = _DB()

    active = await runtime.get_active_web_chat_run(db=db, agent_id=agent_id, session_id=session_id)

    assert active is None
    assert task.status == "completed"
    assert task.result_summary == full_answer
    assert task.completed_at is not None
    assert db.commits == 1


@pytest.mark.asyncio
async def test_claimed_web_chat_run_skips_terminal_transcript_ghost_before_owner(monkeypatch):
    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    task = SimpleNamespace(
        id=run_id,
        tenant_id=tenant_id,
        task_type="web_chat_turn",
        status="running",
        parent_agent_id=agent_id,
        parent_session_id=str(session_id),
        created_at=None,
        started_at=None,
        completed_at=None,
        result_summary=None,
        metadata_json={},
    )
    terminal_event = SimpleNamespace(
        event_type="assistant_message",
        content="final answer",
        metadata_json={"status": "completed"},
    )

    class _DB:
        def __init__(self):
            self.calls = 0
            self.commits = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def execute(self, _stmt):
            self.calls += 1
            return _ScalarResult(task if self.calls == 1 else terminal_event)

        async def commit(self):
            self.commits += 1

    db = _DB()

    async def fake_resolve(*_args, **_kwargs):
        return tenant_id

    monkeypatch.setattr(runtime, "resolve_tenant_for_runtime_task", fake_resolve)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda *a, **k: db)

    reconciled = await runtime._reconcile_claimed_web_chat_terminal_ghost(run_id)

    assert reconciled is True
    assert task.status == "completed"
    assert task.result_summary == "final answer"
    assert task.completed_at is not None


@pytest.mark.asyncio
async def test_execute_web_chat_run_injects_restart_resume_context(monkeypatch):
    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    session_id = "sess-resume"
    runtime_task = SimpleNamespace(
        id=run_id,
        parent_agent_id=agent_id,
        parent_session_id=session_id,
        prompt="continue",
        metadata_json={
            "user_id": str(user_id),
            "session_id": session_id,
            "restart_resume_context": {"resume_prompt": "Resume long task with artifact refs."},
        },
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="Agent",
        role_description="",
        primary_model_id=uuid4(),
        fallback_model_id=None,
        tenant_id=uuid4(),
        agent_type="standard",
    )
    user = SimpleNamespace(id=user_id, display_name="Rocky", username="rocky")
    llm_model = SimpleNamespace(provider="openai", model="gpt-4.1", supports_vision=False)
    captured = {}

    async def fake_load_context(_run_uuid):
        return runtime_task, agent, user, llm_model, None, []

    async def fake_invoke(request):
        captured["system_prompt_suffix"] = request.system_prompt_suffix
        return SimpleNamespace(content="done")

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_load_runtime_context", fake_load_context)
    monkeypatch.setattr(runtime, "_maybe_handle_plan_mode_entry", noop_async)
    monkeypatch.setattr(runtime, "invoke_agent", fake_invoke)
    monkeypatch.setattr(runtime, "_persist_assistant_message", noop_async)
    monkeypatch.setattr(runtime, "_persist_runtime_event", noop_async)
    monkeypatch.setattr(runtime, "_persist_tool_call", noop_async)
    monkeypatch.setattr(runtime, "_update_runtime_task", noop_async)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", noop_async)
    monkeypatch.setattr(runtime, "_resume_queued_plan_handoffs", noop_async)

    await runtime.execute_web_chat_run(run_id)

    assert "Resume long task with artifact refs." in captured["system_prompt_suffix"]


# ---------------------------------------------------------------------------
# Auto-sync gate (§9.0 task auto-sync / §9.2): a regex-detected "create a task"
# must NOT silently background-execute; without a confirmed plan it creates an
# awaiting PlanRequest and tells the user to confirm.
# ---------------------------------------------------------------------------


class _RecommendationSession:
    def __init__(self, recommendation):
        self.recommendation = recommendation
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def execute(self, _stmt):
        return _ScalarResult(self.recommendation)

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_plan_mode_accepts_latest_recommendation_and_activates_interactive_mode(monkeypatch):
    import app.services.web_chat_runtime as runtime

    agent_id = uuid4()
    user_id = uuid4()
    recommendation = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        session_id="sess-accept",
        runtime_task_id=uuid4(),
        recommended_to_user_id=user_id,
        status="recommended",
        original_request="每天 13:00 自动检查 Reddit 帖子并总结投资观点",
        title="每天 13:00 自动检查 Reddit 帖子",
        intent_type="autonomous_wake",
        action_kind="create_enabled_trigger",
        tool_name="set_trigger",
        accepted_by_user_id=None,
        accepted_at=None,
    )
    recommendation_db = _RecommendationSession(recommendation)
    monkeypatch.setattr(runtime, "_async_session", lambda: recommendation_db)
    # RLS 阶段2a/2b: _accept_latest_plan_mode_recommendation resolves the agent's
    # tenant and opens a tenant-scoped session. Route it through the same fake DB
    # and stub the resolver so no real DB / bypass read happens.
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda *_a, **_k: recommendation_db)

    async def _fake_resolve(*_a, **_k):
        return uuid4()

    monkeypatch.setattr("app.services.tenant_resolver.resolve_tenant_for_agent", _fake_resolve)

    session_context = SimpleNamespace(metadata={})
    response = await runtime._maybe_handle_plan_mode_entry(
        agent_id=agent_id,
        user_id=user_id,
        session_id="sess-accept",
        content="进入计划模式",
        runtime_session_context=session_context,
    )

    assert response is None
    assert recommendation.status == "accepted"
    assert recommendation.accepted_by_user_id == user_id
    metadata = session_context.metadata["plan_mode"]
    assert metadata["original_request"] == recommendation.original_request
    assert metadata["intent_type"] == "autonomous_wake"
    assert metadata["action_kind"] == "create_enabled_trigger"
    assert metadata["tool_name"] == "set_trigger"


@pytest.mark.asyncio
async def test_maybe_handle_plan_mode_entry_does_not_pre_empt_schedule_intent():
    """P0-5: schedule wording no longer pre-empts the turn with a canned template.
    classify returns 'none' and the entry handler falls through (returns None) so
    the agent handles the message and suggests Plan Mode in its own reply."""
    import app.services.web_chat_runtime as runtime

    result = await runtime._maybe_handle_plan_mode_entry(
        agent_id=uuid4(),
        user_id=uuid4(),
        session_id="session-1",
        content="每天 9 点帮我整理新闻",
        plan_mode_requested=False,
    )

    assert result is None  # no canned recommendation; falls through to the agent


@pytest.mark.asyncio
async def test_maybe_handle_plan_mode_entry_activates_interactive_mode_when_explicitly_requested(monkeypatch):
    import app.services.web_chat_runtime as runtime

    session_context = SimpleNamespace(metadata={})

    result = await runtime._maybe_handle_plan_mode_entry(
        agent_id=uuid4(),
        user_id=uuid4(),
        session_id="session-1",
        content="帮我完整调研这个行业",
        plan_mode_requested=True,
        runtime_session_context=session_context,
    )

    assert result is None
    assert session_context.metadata["plan_mode"]["active"] is True
    assert session_context.metadata["plan_mode"]["original_request"] == "帮我完整调研这个行业"
    assert session_context.metadata["plan_mode"]["intent_type"] == "in_session_execution"
    assert session_context.metadata["plan_mode"]["action_kind"] == "start_long_task"
    # CC-align §4.2: a normal live-chat plan defaults to continuing in THIS session
    # after confirmation — NOT the old detached ``long_task`` (which had no handler
    # and resolved to skipped). intent_type stays long_task; only the target moved.
    assert session_context.metadata["plan_mode"]["handoff_target"] == "continue_current_session"


@pytest.mark.asyncio
async def test_maybe_handle_plan_mode_entry_classifies_visible_request_not_attachment_body(monkeypatch):
    import app.services.web_chat_runtime as runtime

    session_context = SimpleNamespace(metadata={})

    result = await runtime._maybe_handle_plan_mode_entry(
        agent_id=uuid4(),
        user_id=uuid4(),
        session_id="session-file-update",
        content=(
            "[File: bank_statement.pdf]\n"
            "Monthly statement body text extracted from the uploaded PDF.\n\n"
            "Question: 帮我更新到5月"
        ),
        classification_content="[📎 bank_statement.pdf]\n帮我更新到5月",
        plan_mode_requested=True,
        runtime_session_context=session_context,
    )

    assert result is None
    state = session_context.metadata["plan_mode"]
    assert state["intent_type"] == "in_session_execution"
    assert state["action_kind"] == "start_long_task"
    assert state["tool_name"] == "continue_current_session"
    assert state["handoff_target"] == "continue_current_session"
    assert state["original_request"].startswith("[File: bank_statement.pdf]")


@pytest.mark.asyncio
async def test_activate_interactive_plan_mode_writes_typed_state_and_keeps_dict_mirror(monkeypatch):
    """Phase 1: a real SessionContext gets the typed PlanModeState populated,
    and the legacy metadata['plan_mode'] dict stays a byte-exact mirror."""
    import app.services.web_chat_runtime as runtime
    from app.runtime.session import SessionContext

    session_context = SessionContext()

    result = await runtime._maybe_handle_plan_mode_entry(
        agent_id=uuid4(),
        user_id=uuid4(),
        session_id="session-1",
        content="帮我完整调研这个行业",
        plan_mode_requested=True,
        runtime_session_context=session_context,
    )

    assert result is None
    # Typed source of truth populated.
    assert session_context.plan_mode.active is True
    assert session_context.plan_mode.original_request == "帮我完整调研这个行业"
    assert session_context.plan_mode.intent_type == "in_session_execution"
    # Legacy dict mirror stays consistent with the typed state.
    assert session_context.metadata["plan_mode"] == session_context.plan_mode.to_metadata()
    # Runtime-only injection bookkeeping never leaks into the mirror.
    assert "reminded_full" not in session_context.metadata["plan_mode"]
    # Phase 4B: a session-scoped plan file is provisioned and mirrored for the gate.
    assert session_context.plan_mode.plan_file_path == "workspace/plans/session-1.plan.md"
    assert session_context.metadata["plan_mode"]["plan_file_path"] == "workspace/plans/session-1.plan.md"


@pytest.mark.asyncio
async def test_activate_interactive_plan_mode_provisions_markdown_plan_file(tmp_path, monkeypatch):
    """MD-first Plan Mode reserves the exact path without an unowned file.

    The first governed write creates both the file and its resource manifest;
    an empty pre-created file would be rejected by workspace ownership checks.
    """
    import app.services.web_chat_runtime as runtime
    from app.runtime.session import SessionContext

    agent_id = uuid4()
    monkeypatch.setattr(runtime, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    session_context = SessionContext()

    result = await runtime._maybe_handle_plan_mode_entry(
        agent_id=agent_id,
        user_id=uuid4(),
        session_id="session-1",
        content="进入计划模式，做一个关于跨链桥的报告",
        plan_mode_requested=True,
        runtime_session_context=session_context,
    )

    assert result is None
    plan_path = tmp_path / str(agent_id) / "workspace" / "plans" / "session-1.plan.md"
    assert plan_path.parent.is_dir()
    assert not plan_path.exists()


@pytest.mark.asyncio
async def test_maybe_handle_plan_mode_entry_activates_interactive_plan(monkeypatch):
    import app.services.web_chat_runtime as runtime

    session_context = SimpleNamespace(metadata={})

    result = await runtime._maybe_handle_plan_mode_entry(
        agent_id=uuid4(),
        user_id=uuid4(),
        session_id="session-1",
        content="做一个web3的全景报告",
        plan_mode_requested=True,
        runtime_session_context=session_context,
    )

    assert result is None
    assert session_context.metadata["plan_mode"]["active"] is True
    assert session_context.metadata["plan_mode"]["handoff_target"] == "continue_current_session"


@pytest.mark.asyncio
async def test_maybe_handle_plan_mode_entry_does_not_auto_enter_without_explicit_request(monkeypatch):
    """Without an explicit request the agent's judgment never triggers entry."""
    import app.services.web_chat_runtime as runtime

    session_context = SimpleNamespace(metadata={})

    result = await runtime._maybe_handle_plan_mode_entry(
        agent_id=uuid4(),
        user_id=uuid4(),
        session_id="session-1",
        content="做一个web3的全景报告",
        plan_mode_requested=False,
        runtime_session_context=session_context,
    )

    assert result is None
    assert "plan_mode" not in session_context.metadata


@pytest.mark.parametrize(
    ("invoke_error", "expected_reason"),
    [
        pytest.param(
            LLMError("provider stream closed"),
            "provider_error",
            id="provider",
        ),
        pytest.param(
            SQLAlchemyError("database unavailable"),
            "persistence_error",
            id="persistence",
        ),
    ],
)
@pytest.mark.asyncio
async def test_execute_web_chat_run_persists_typed_failure_without_platform_authored_assistant(
    monkeypatch,
    invoke_error: Exception,
    expected_reason: str,
):
    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    runtime_task = SimpleNamespace(
        id=run_id,
        parent_agent_id=agent_id,
        parent_session_id=session_id,
        prompt="latest question",
        metadata_json={"user_id": str(user_id), "session_id": session_id},
        trace_id=None,
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="Web3研究员",
        role_description="",
        primary_model_id=uuid4(),
        fallback_model_id=None,
        tenant_id=uuid4(),
        agent_type="native",
    )
    user = SimpleNamespace(id=user_id, display_name="Rocky", username="rocky")
    llm_model = SimpleNamespace(provider="openai", model="gpt-4.1", supports_vision=False)
    finalized_without_assistant: list[dict] = []
    updates: list[tuple] = []
    broadcasts: list[dict] = []

    async def fake_load_context(_run_uuid):
        return runtime_task, agent, user, llm_model, None, []

    async def fake_invoke(_request):
        raise invoke_error

    async def fail_assistant_finalize(**_kwargs):
        raise AssertionError("runtime infrastructure failure must not become assistant prose")

    async def fake_finalize_without_assistant(**kwargs):
        finalized_without_assistant.append(kwargs)
        return True

    async def fake_update(run_uuid, **kwargs):
        updates.append((run_uuid, kwargs))

    async def fake_broadcast(_agent_id, _session_id, event):
        broadcasts.append(event)

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_load_runtime_context", fake_load_context)
    monkeypatch.setattr(runtime, "_maybe_handle_plan_mode_entry", noop_async)
    monkeypatch.setattr(runtime, "invoke_agent", fake_invoke)
    monkeypatch.setattr(runtime, "_finalize_web_chat_run_with_assistant", fail_assistant_finalize)
    monkeypatch.setattr(runtime, "_finalize_web_chat_run_without_assistant", fake_finalize_without_assistant)
    monkeypatch.setattr(runtime, "_update_runtime_task", fake_update)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)
    monkeypatch.setattr(runtime, "_resume_queued_plan_handoffs", noop_async)

    await runtime.execute_web_chat_run(run_id)

    assert finalized_without_assistant == [
        {
            "run_uuid": run_id,
            "agent_id": agent_id,
            "session_id": session_id,
            "status": "failed",
            "result_summary": f"Web chat run failed: {type(invoke_error).__name__}",
            "metadata_json": {"error": str(invoke_error), "terminal_reason": expected_reason},
        }
    ]
    assert updates == []
    assert {
        "type": "runtime_failure",
        "status": "failed",
        "reason": expected_reason,
        "retryable": True,
    } in broadcasts
    assert all("content" not in event for event in broadcasts if event.get("type") == "runtime_failure")
    # The RuntimePhase backstop settles the stream after the visible error event.
    phase_broadcasts = [event for event in broadcasts if event.get("type") == "phase"]
    assert phase_broadcasts[-1]["phase"] == "failed"


@pytest.mark.asyncio
async def test_execute_web_chat_run_does_not_deliver_web_turn_to_historical_im_target(monkeypatch):
    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    runtime_task = SimpleNamespace(
        id=run_id,
        parent_agent_id=agent_id,
        parent_session_id=session_id,
        prompt="web follow-up",
        metadata_json={"user_id": str(user_id), "session_id": session_id, "source": "web"},
        trace_id=f"web_chat_turn:{run_id.hex}",
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="Agent",
        role_description="",
        primary_model_id=uuid4(),
        fallback_model_id=None,
        tenant_id=uuid4(),
        agent_type="native",
    )
    user = SimpleNamespace(id=user_id, display_name="Rocky", username="rocky")
    llm_model = SimpleNamespace(provider="openai", model="gpt-4.1", supports_vision=False)
    session = SimpleNamespace(delivery_target_json={"channel": "feishu", "chat_id": "oc_x"})
    finalized = {"n": 0}

    async def fake_load_context(_run_uuid):
        return runtime_task, agent, user, llm_model, None, [], session

    async def fake_invoke(_request):
        return SimpleNamespace(content="web answer", reasoning_signature=None)

    async def fake_finalize(**_kwargs):
        finalized["n"] += 1
        return True

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_load_runtime_context", fake_load_context)
    monkeypatch.setattr(runtime, "_maybe_handle_plan_mode_entry", noop_async)
    monkeypatch.setattr(runtime, "invoke_agent", fake_invoke)
    monkeypatch.setattr(runtime, "_finalize_web_chat_run_with_assistant", fake_finalize)
    monkeypatch.setattr(runtime, "_persist_runtime_event", noop_async)
    monkeypatch.setattr(runtime, "_persist_tool_call", noop_async)
    monkeypatch.setattr(runtime, "_update_runtime_task", noop_async)
    monkeypatch.setattr(runtime, "_emit_terminal_turn_hook", noop_async)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", noop_async)
    monkeypatch.setattr(runtime, "_resume_queued_plan_handoffs", noop_async)

    await runtime.execute_web_chat_run(run_id)

    assert finalized["n"] == 1


def test_active_channel_delivery_target_requires_typed_turn_authorization():
    import app.services.web_chat_runtime as runtime

    target = {"channel": "feishu", "chat_id": "oc_x"}
    session = SimpleNamespace(delivery_target_json=target)
    context = SimpleNamespace(source="web")

    assert (
        runtime._active_channel_delivery_target_for_turn(
            metadata={"source": "web"},
            runtime_session_context=context,
            session=session,
            prompt="请把这条消息发送到飞书",
        )
        is None
    )
    assert (
        runtime._active_channel_delivery_target_for_turn(
            metadata={"source": "web", "allow_channel_delivery_tools": True},
            runtime_session_context=context,
            session=session,
            prompt="ordinary prose without channel keywords",
        )
        == target
    )


@pytest.mark.asyncio
async def test_execute_web_chat_run_keeps_cancelled_exception_as_killed(monkeypatch):
    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    cancel_event = asyncio.Event()
    cancel_event.set()
    updates = []

    async def fake_load_context(_run_uuid):
        raise RuntimeError("cancelled by runtime")

    async def fake_update(run_uuid, **kwargs):
        updates.append((run_uuid, kwargs))

    monkeypatch.setattr(runtime, "_load_runtime_context", fake_load_context)
    monkeypatch.setattr(runtime, "_update_runtime_task", fake_update)

    await runtime.execute_web_chat_run(run_id, cancel_event=cancel_event)

    assert updates == [
        (
            run_id,
            {
                "status": "killed",
                "result_summary": "Generation stopped by user.",
                "metadata_json": {"cancelled_by_user": True},
            },
        )
    ]


@pytest.mark.asyncio
async def test_terminal_channel_result_is_enqueued_instead_of_sent_inline(monkeypatch):
    import app.services.channel_delivery_outbox as outbox
    import app.services.web_chat_runtime as runtime

    tenant_id = uuid4()
    agent_id = uuid4()
    session_id = uuid4()
    run_id = uuid4()
    user_id = uuid4()
    config_id = uuid4()
    target = {"channel": "feishu", "chat_id": "oc_x"}
    task = SimpleNamespace(id=run_id, tenant_id=tenant_id, metadata_json={"source": "feishu"})
    session = SimpleNamespace(delivery_target_json=target)
    config = SimpleNamespace(id=config_id)

    class _Result:
        def __init__(self, value=None, values=None):
            self.value = value
            self.values = values or []

        def scalar_one_or_none(self):
            return self.value

        def scalars(self):
            return SimpleNamespace(all=lambda: list(self.values))

    class _DB:
        def __init__(self):
            self.results = [_Result(session), _Result(config), _Result(values=[])]

        async def execute(self, _stmt):
            return self.results.pop(0)

    captured = {}

    async def fake_enqueue(_db, intent):
        captured["intent"] = intent
        return uuid4()

    monkeypatch.setattr(outbox, "enqueue_channel_delivery", fake_enqueue)
    await runtime._enqueue_terminal_channel_delivery(
        db=_DB(),
        task=task,
        agent_id=agent_id,
        session_id=str(session_id),
        user_id=user_id,
        external_principal_id=None,
        content="执行完成",
        status="completed",
        artifact_parts=[],
        metadata_json={"turn_id": "turn-1"},
    )

    intent = captured["intent"]
    assert intent.delivery_target == target
    assert intent.text == "执行完成"
    assert intent.channel_config_id == config_id
    assert intent.runtime_task_id == run_id


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [None, {"channel": "web", "username": "alice"}])
async def test_terminal_channel_result_skips_non_external_delivery_targets(monkeypatch, target):
    import app.services.channel_delivery_outbox as outbox
    import app.services.web_chat_runtime as runtime

    session = SimpleNamespace(delivery_target_json=target)

    class _DB:
        async def execute(self, _stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: session)

    async def fail_enqueue(*_args, **_kwargs):
        raise AssertionError("web/no-target session must not enqueue external delivery")

    monkeypatch.setattr(outbox, "enqueue_channel_delivery", fail_enqueue)
    result = await runtime._enqueue_terminal_channel_delivery(
        db=_DB(),
        task=SimpleNamespace(id=uuid4(), tenant_id=uuid4()),
        agent_id=uuid4(),
        session_id=str(uuid4()),
        user_id=uuid4(),
        external_principal_id=None,
        content="执行完成",
        status="completed",
        artifact_parts=[],
        metadata_json={},
    )
    assert result is None


@pytest.mark.asyncio
async def test_web_chat_stream_micro_batcher_coalesces_chunk_bursts():
    import app.services.web_chat_runtime as runtime

    sent: list[tuple[str, str, bool]] = []
    now = [100.0]

    async def send(kind: str, text: str, *, reset: bool = False) -> None:
        sent.append((kind, text, reset))

    batcher = runtime._WebChatStreamMicroBatcher(
        send,
        flush_interval_seconds=10,
        max_chars=100,
        clock=lambda: now[0],
    )

    await batcher.emit_chunk("hel")
    await batcher.emit_chunk("lo")

    assert sent == []

    await batcher.flush()

    assert sent == [("chunk", "hello", False)]


@pytest.mark.asyncio
async def test_web_chat_stream_micro_batcher_flushes_sparse_delta_after_interval():
    import app.services.web_chat_runtime as runtime

    sent: list[tuple[str, str, bool]] = []

    async def send(kind: str, text: str, *, reset: bool = False) -> None:
        sent.append((kind, text, reset))

    batcher = runtime._WebChatStreamMicroBatcher(
        send,
        flush_interval_seconds=0.001,
        max_chars=100,
    )

    await batcher.emit_chunk("slow")
    await asyncio.sleep(0.02)

    assert sent == [("chunk", "slow", False)]


@pytest.mark.asyncio
async def test_web_chat_stream_micro_batcher_flushes_before_reset_and_preserves_order():
    import app.services.web_chat_runtime as runtime

    sent: list[tuple[str, str, bool]] = []

    async def send(kind: str, text: str, *, reset: bool = False) -> None:
        sent.append((kind, text, reset))

    batcher = runtime._WebChatStreamMicroBatcher(
        send,
        flush_interval_seconds=10,
        max_chars=100,
        clock=lambda: 100.0,
    )

    await batcher.emit_chunk("a")
    await batcher.emit_thinking("think")
    await batcher.emit_chunk("b")
    await batcher.reset_chunk()

    assert sent == [
        ("chunk", "a", False),
        ("thinking", "think", False),
        ("chunk", "b", False),
        ("chunk", "", True),
    ]


def test_memory_context_degradation_events_are_durable_session_context_events() -> None:
    import inspect

    from app.services import web_chat_run_orchestrator
    from app.services.web_chat_runtime import _should_persist_runtime_event

    assert _should_persist_runtime_event(
        {"type": "session_context", "event_type": "memory_context_degraded", "retryable": True}
    )
    assert _should_persist_runtime_event(
        {"type": "session_context", "event_type": "memory_context_unavailable", "retryable": True}
    )
    source = inspect.getsource(web_chat_run_orchestrator._WebChatCallbacks.runtime_event)
    assert source.index("await events.persist_runtime_event") < source.index("await events.broadcast")
