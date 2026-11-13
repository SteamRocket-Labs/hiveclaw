"""X3c — expanded rows name the file, the host and the connected app.

Run 19 proved the member view never sees the arguments: a native tool_call
payload carries only ``args_hash`` and a native tool_result payload has no
``args`` at all, so X3b's read-side derivation from ``payload['args']`` could
never fire in production. X3c stores a small typed ``member_target`` on the
native tool_result payload at persistence time (the one place the typed
arguments are known) and the member summary reads that target.

These pins cover the production payload shapes copied from run 19's
``api-evidence.ndjson``, the real persistence path, and the model-input proof.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from tests.services.test_session_semantic_history import (
    _bind_round_one, _seed, _seal_round, _submit_turn,
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")

from uuid import uuid4

PAGE_BODY = "# React Wiki\n[Syntax error in textmermaid version 11.16.1]\nraw page bytes"


def _native_row(
    name: str,
    result: str,
    *,
    outcome: str = "success",
    member_target: dict[str, Any] | None = None,
) -> SimpleNamespace:
    """A native V2 tool_result row exactly as production serializes its member
    transcript: no ``args`` key, tool_name/outcome typed, the X3c member_target
    present when the write path derived one (run 19 api-evidence shape)."""

    payload: dict[str, Any] = {
        "invocation_id": "inv-x3c",
        "provider_request_id": "req-x3c",
        "provider_tool_use_id": "ptu-x3c",
        "tool_name": name,
        "outcome": outcome,
        "retryable": False,
        "content": result,
        "content_hash": "h" * 8,
        "content_or_error_ref": "receipt:x3c",
        "parts": [],
    }
    if member_target is not None:
        payload["member_target"] = member_target
    return SimpleNamespace(
        id=uuid4(),
        sequence=42,
        tenant_id=uuid4(),
        schema_version=2,
        scope_json={"level": "round", "session_id": "s1", "thread_id": "s1", "turn_id": "tu", "run_id": "ru", "round_id": "ro"},
        item_id=str(uuid4()),
        item_kind="tool_result",
        event_type="tool_result.completed",
        lifecycle="completed",
        payload_schema="hive.session.payload.tool_result.completed.v2",
        actor_type="tool",
        visibility_scope="direct_user",
        run_id="ru",
        message_id="m1",
        invocation_id="inv-x3c",
        provider_tool_use_id="ptu-x3c",
        content_hash="h" * 8,
        created_at="2026-11-13T10:00:00Z",
        metadata_json={
            "actor": {"type": "tool"},
            "visibility": {"audience": "direct_user"},
            "v2_persisted_at": "2026-11-13T10:00:00Z",
            "v2_payload": payload,
        },
    )


def _user_payload(row: SimpleNamespace) -> dict:
    from app.services.session_event_contract import serialize_session_event

    return serialize_session_event(row, audience="user")["payload"]


def _operator_payload(row: SimpleNamespace) -> dict:
    from app.services.session_event_contract import serialize_session_event

    return serialize_session_event(row, audience="operator")["payload"]


def test_x3c_native_fetch_row_names_the_host_from_the_typed_target() -> None:
    # Production shape: firecrawl_fetch tool_result without args, with the
    # member_target the persistence path derived from the typed url argument.
    row = _native_row("firecrawl_fetch", PAGE_BODY, member_target={"host": "deepwiki.com"})
    before = json.dumps(_operator_payload(row), ensure_ascii=False, sort_keys=True)
    payload = _user_payload(row)
    assert payload["member_summary"] == {"kind": "web_fetch", "state": "done", "host": "deepwiki.com"}
    assert payload["content"] == ""
    assert "Syntax error" not in json.dumps(payload, ensure_ascii=False)
    # The operator envelope keeps the exact durable bytes; only the member view
    # projects. The stored payload itself still carries the typed target.
    assert json.dumps(_operator_payload(row), ensure_ascii=False, sort_keys=True) == before


def test_x3c_failed_fetch_names_the_host_that_did_not_open() -> None:
    row = _native_row("web_fetch", "partial bytes", outcome="failed", member_target={"host": "example.org"})
    payload = _user_payload(row)
    assert payload["member_summary"] == {"kind": "web_fetch", "state": "not_done", "host": "example.org"}


def test_x3c_native_file_read_names_the_basename_from_the_typed_target() -> None:
    row = _native_row("read_file", "date,sales\n2026,35", member_target={"filename": "sales-run19.csv"})
    payload = _user_payload(row)
    assert payload["member_summary"] == {"kind": "file_read", "state": "done", "filename": "sales-run19.csv"}
    assert "2026,35" not in json.dumps(payload, ensure_ascii=False)


@pytest.mark.parametrize("tool,kind", [("write_file", "file_write"), ("fs_write", "file_write"), ("edit_file", "file_edit")])
def test_x3c_write_and_edit_rows_name_the_file(tool: str, kind: str) -> None:
    # X3b left write/edit rows on the generic action.status sentence; X3c
    # summarizes them like reads, with the base name only.
    row = _native_row(tool, "Write confirmed.", member_target={"filename": "weekly-report.md"})
    payload = _user_payload(row)
    assert payload["member_summary"] == {"kind": kind, "state": "done", "filename": "weekly-report.md"}
    assert "Write confirmed." not in json.dumps(payload, ensure_ascii=False)
    # A refused write is not done.
    failed = _native_row(tool, "denied", outcome="denied", member_target={"filename": "weekly-report.md"})
    assert _user_payload(failed)["member_summary"]["state"] == "not_done"


def test_x3c_company_mcp_row_names_the_trusted_connection_label() -> None:
    # Run 19's DeepWiki rows (mcp_ask_wiki_question) showed only
    # 「使用已连接的应用」. X3c: the row names the trusted connection label
    # G4c uses; the remote tool name and description never enter the summary.
    body = json.dumps({"answer": "remote body with mcp_ask_wiki_question ids", "remote_tool": "ask_wiki_question"})
    row = _native_row("mcp_ask_wiki_question", body, member_target={"app_label": "DeepWiki"})
    before = json.dumps(_operator_payload(row), ensure_ascii=False, sort_keys=True)
    payload = _user_payload(row)
    assert payload["member_summary"] == {"kind": "mcp_app", "state": "done", "app_label": "DeepWiki"}
    assert payload["content"] == ""
    assert "remote body" not in json.dumps(payload, ensure_ascii=False)
    assert json.dumps(_operator_payload(row), ensure_ascii=False, sort_keys=True) == before
    # A failed MCP call keeps the label with the not-done state.
    failed = _native_row("mcp_ask_wiki_question", "upstream error", outcome="failed", member_target={"app_label": "DeepWiki"})
    assert _user_payload(failed)["member_summary"] == {
        "kind": "mcp_app", "state": "not_done", "app_label": "DeepWiki",
    }


def test_x3c_old_history_rows_keep_the_generic_sentence() -> None:
    # Requirement: rows written before this change carry no member_target and
    # no args; they must keep X3b's generic sentence and never gain a name.
    row = _native_row("read_file", "date,sales\n2026,35")
    payload = _user_payload(row)
    assert payload["member_summary"] == {"kind": "file_read", "state": "done"}
    fetch = _native_row("web_fetch", PAGE_BODY)
    assert _user_payload(fetch)["member_summary"] == {"kind": "web_fetch", "state": "done"}


def test_x3c_args_still_win_over_the_stored_target() -> None:
    # Legacy/compat rows and any payload carrying the real typed arguments
    # derive from the arguments themselves (the actual call), never from a
    # stale stored target.
    row = _native_row("web_fetch", PAGE_BODY, member_target={"host": "stale.example"})
    row.metadata_json["v2_payload"]["args"] = {"url": "https://fresh.example/path"}
    payload = _user_payload(row)
    assert payload["member_summary"]["host"] == "fresh.example"


def test_x3c_member_target_filename_keeps_the_member_safe_rules() -> None:
    # The stored target is platform-written, but the read side still refuses a
    # UUID-shaped or directory-carrying name it did not expect.
    row = _native_row("read_file", "x", member_target={"filename": "120e98ce-d9e6-5e29-a707-7992e369a745.md"})
    assert "filename" not in _user_payload(row)["member_summary"]


async def _settled_invocation(
    owner_sessionmaker,
    tool_name: str,
    content: str,
    *,
    arguments: dict,
    execution_evidence_extra: dict | None = None,
    mcp_tool_row: dict | None = None,
):
    """The real persistence path: prepare -> effect started -> complete."""

    from app.services.session_tool_runtime import (
        _sha256, complete_tool_invocation, mark_tool_effect_started, prepare_tool_invocation,
    )

    tenant, user, agent, session = await _seed(owner_sessionmaker)
    if mcp_tool_row is not None:
        from app.models.tool import Tool

        async with owner_sessionmaker() as db:
            db.add(Tool(tenant_id=tenant, type="mcp", **mcp_tool_row))
            await db.commit()
    _, run, turn = await _submit_turn(owner_sessionmaker, user_id=user, agent_id=agent,
        session_id=session, content="marker")
    common = dict(tenant_id=tenant, agent_id=agent, session_id=session, run_id=run)
    await _bind_round_one(owner_sessionmaker, **common, turn_id=turn)
    async with owner_sessionmaker() as db:
        from app.models.session_v2 import SessionTurnInput
        from app.services.session_model_round import prepare_model_request

        input_id = await db.scalar(select(SessionTurnInput.id).where(SessionTurnInput.session_id == session))
        request = await prepare_model_request(db, **common, turn_id=turn, round_index=1,
            messages=[{"role": "user", "content": "marker", "source_ref": f"session-input:{input_id}"}],
            tools=None, provider="openai", model="gpt-test")
        invocation = await prepare_tool_invocation(db, **common, provider_request_id=request,
            provider_tool_use_id="x3c-call", tool_name=tool_name, arguments=arguments)
        await mark_tool_effect_started(db, tenant_id=tenant, agent_id=agent, session_id=session,
            invocation_id=invocation.id)
        await complete_tool_invocation(db, tenant_id=tenant, agent_id=agent, session_id=session,
            invocation_id=invocation.id, provider_result_content=content, execution_evidence={
                "schema": "hive.tool_execution_evidence.v1", "status": "settled", "retryable": False,
                "pre_effect_fence_ref": "x3c-result-test",
                **(execution_evidence_extra or {}),
                "tool_decision": {"schema": "hive.tool_decision.v1", "decision_id": "x3c",
                    "outcome": "allow", "input_hash": invocation.args_hash,
                    "policy_snapshot_hash": "a" * 64, "capability_snapshot_hash": "b" * 64},
                "execution_frame": {"status": "completed", "output_hash": _sha256(content)},
            })
        await _seal_round(db, **common, turn_id=turn, round_index=1, provider_request_id=request,
            content="", tool_calls=[{"id": "x3c-call", "name": tool_name, "arguments": arguments}])
        await db.commit()
        return tenant, agent, session, invocation.id


async def _result_payload(owner_sessionmaker, invocation_id) -> dict:
    from app.models.chat_transcript_event import ChatTranscriptEvent

    async with owner_sessionmaker() as db:
        event = await db.scalar(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.invocation_id == invocation_id,
            ChatTranscriptEvent.event_type == "tool_result.completed",
        ))
        assert event is not None, "fixture_did_not_settle_a_tool_result"
        return event.metadata_json["v2_payload"]


@pytest.mark.asyncio
async def test_x3c_persistence_stores_the_typed_file_target(owner_sessionmaker) -> None:
    # The real path: a tool call goes in, the transcript event comes out with
    # the typed target derived from the typed arguments.
    _, _, _, invocation_id = await _settled_invocation(
        owner_sessionmaker, "read_file", 'print("source")\n' * 10,
        arguments={"path": "memory/t0/sessions/s/workspace/uploads/sales-run19.csv"},
    )
    payload = await _result_payload(owner_sessionmaker, invocation_id)
    assert payload["member_target"] == {"filename": "sales-run19.csv"}


@pytest.mark.asyncio
async def test_x3c_persistence_stores_the_typed_host_target(owner_sessionmaker) -> None:
    _, _, _, invocation_id = await _settled_invocation(
        owner_sessionmaker, "web_fetch", PAGE_BODY,
        arguments={"url": "https://deepwiki.com/react/python"},
    )
    payload = await _result_payload(owner_sessionmaker, invocation_id)
    assert payload["member_target"] == {"host": "deepwiki.com"}


@pytest.mark.asyncio
async def test_x3c_persistence_stores_the_trusted_mcp_connection_label(owner_sessionmaker) -> None:
    # Tool.type == 'mcp' is the typed decision; the label is the same
    # trusted_connection_label G4c uses, resolved at write time only.
    _, _, _, invocation_id = await _settled_invocation(
        owner_sessionmaker, "mcp_ask_wiki_question", json.dumps({"answer": "remote"}),
        arguments={"repoName": "facebook/react", "question": "how?"},
        mcp_tool_row={
            "id": uuid.uuid4(), "name": "mcp_ask_wiki_question", "display_name": "DeepWiki ask",
            "description": "ask", "mcp_server_name": "DeepWiki", "mcp_server_url": "https://deepwiki.com",
            "mcp_tool_name": "ask_wiki_question",
        },
    )
    payload = await _result_payload(owner_sessionmaker, invocation_id)
    assert payload["member_target"] == {"app_label": "DeepWiki"}


@pytest.mark.asyncio
async def test_x3c_persistence_keeps_the_typed_command_result(owner_sessionmaker) -> None:
    # Run 19's failed command row (exit_code 1 on a success outcome) already
    # persists the typed code_execution_result on both events; pin the field
    # the member status decision reads.
    from app.models.chat_transcript_event import ChatTranscriptEvent

    _, _, _, invocation_id = await _settled_invocation(
        owner_sessionmaker, "execute_code", "Traceback (most recent call last): KeyError",
        arguments={"language": "python", "code": "raise KeyError('x')"},
        execution_evidence_extra={"code_execution_result": {"exit_code": 1, "timed_out": False}},
    )
    async with owner_sessionmaker() as db:
        events = (await db.execute(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.invocation_id == invocation_id,
        ))).scalars().all()
    by_type = {event.event_type: event.metadata_json["v2_payload"] for event in events}
    assert by_type["tool_result.completed"]["code_execution_result"] == {"exit_code": 1, "timed_out": False}
    assert by_type["tool_call.completed"]["code_execution_result"] == {"exit_code": 1, "timed_out": False}
    # Commands are not registry-summarized; no member_target is stored.
    assert "member_target" not in by_type["tool_result.completed"]


@pytest.mark.asyncio
async def test_x3c_model_input_bytes_unchanged_by_the_typed_target(owner_sessionmaker) -> None:
    # The model-input proof on the real history-replay path: after the write
    # path stores member_target, the replayed provider messages are exactly
    # the durable result bytes — the new field reaches neither the replayed
    # content nor any provider message field.
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services.session_event_contract import serialize_session_event
    from app.services.session_semantic_history import load_session_semantic_history

    content = 'print("ordinary source")\n' * 50
    tenant, agent, session, invocation_id = await _settled_invocation(
        owner_sessionmaker, "read_file", content,
        arguments={"path": "memory/t0/sessions/s/workspace/uploads/sales-run19.csv"},
    )
    async with owner_sessionmaker() as db:
        event = await db.scalar(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.invocation_id == invocation_id,
            ChatTranscriptEvent.event_type == "tool_result.completed",
        ))
        durable_payload = event.metadata_json["v2_payload"]
        assert durable_payload["member_target"] == {"filename": "sales-run19.csv"}
        row = SimpleNamespace(
            id=event.id, sequence=event.sequence, tenant_id=event.tenant_id, schema_version=2,
            scope_json=event.scope_json, item_id=event.item_id or str(event.id), item_kind=event.item_kind,
            event_type=event.event_type, lifecycle=event.lifecycle, payload_schema=event.payload_schema,
            actor_type=event.actor_type, visibility_scope=event.visibility_scope, run_id=event.run_id,
            message_id=event.message_id, invocation_id=event.invocation_id,
            provider_tool_use_id=getattr(event, "provider_tool_use_id", None),
            content_hash=getattr(event, "content_hash", None), created_at=str(event.created_at),
            metadata_json=event.metadata_json,
        )
        user_view = serialize_session_event(row, audience="user")["payload"]
        assert user_view["member_summary"]["filename"] == "sales-run19.csv"
        assert user_view["content"] == ""
        history = await load_session_semantic_history(db, tenant_id=tenant, agent_id=agent, session_id=session)
        tool_messages = [message for message in history.messages if message.role == "tool"]
        assert tool_messages, "fixture_did_not_produce_tool_replay"
        # Byte identity: every replayed tool message is the exact durable
        # content, and the typed target appears in no provider message bytes.
        assert all(str(message.content) == content for message in tool_messages)
        assert "member_target" not in json.dumps(
            [dataclasses.asdict(message) for message in history.messages], ensure_ascii=False, default=str
        )
