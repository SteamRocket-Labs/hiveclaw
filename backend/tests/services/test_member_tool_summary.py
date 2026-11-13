"""X3 — one plain sentence per expanded member step for summarized tools.

The member projection (`serialize_session_event(..., audience="user")`, the
single seam behind the live socket, SSE subscription, hydration, reconcile and
group history) must drop the raw result bytes for the X3 tool set and carry a
typed summary instead. Decided by tool name and typed fields only; the operator
envelope and the persisted row — the model's input — stay byte-identical.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.session_event_contract import (
    MEMBER_TOOL_SUMMARY_KINDS,
    serialize_session_event,
)

MARKER = "不要向用户展示"
MEMORY_ID = "explicit_284ff1e71ca75b9f92800d2c6fc40ab9"
INVOCATION_UUID = "4f10ca36-8bed-5bc0-88f2-ff509041ab08"
MEMORY_BODY = (
    "## 读取到的记忆\n"
    f"- 写周报时只用三条要点，每条不超过二十个字。\n"
    f"  来源参考（仅供核对，{MARKER}）：\"session:2026-11-13 user instruction [E2E], "
    f"session-tool-invocation:{INVOCATION_UUID}\"\n"
    f"  管理引用（仅用于读取、更正或忘记，{MARKER}）：id={MEMORY_ID}\n"
    "## 可用情况\n目前只读到了部分记忆，暂不可用的部分不能用来下结论。"
)
FIRECRAWL_BODY = (
    "# React Wiki\n[Syntax error in textmermaid version 11.16.1]\n"
    "[Packages](/packages) · [react](/packages/react) · monorepo/Yarn workspaces"
)


def _tool_result_row(name: str, args: dict, result: str, *, status: str = "done") -> SimpleNamespace:
    """A persisted tool_result row exactly as web_chat_runtime writes it.

    The runtime persists the vendor-neutral envelope JSON in ``content`` and
    typed facts in metadata; V1 rows carry no round_id, so they serialize
    through the compatibility branch — the shape the member chat really gets.
    """

    envelope = {
        "name": name,
        "args": args,
        "status": status,
        "tool_call_id": "call_x3",
        "visibility": "collapsed",
        "result": result,
    }
    return SimpleNamespace(
        id=uuid4(),
        sequence=41,
        tenant_id=uuid4(),
        session_id=uuid4(),
        agent_id=uuid4(),
        run_id=uuid4(),
        parent_event_id=None,
        root_session_id=None,
        parent_session_id=None,
        message_id=uuid4(),
        schema_version=1,
        item_type="tool_result",
        item_status="succeeded",
        turn_id=str(uuid4()),
        causation_id=None,
        correlation_id=None,
        actor_type="tool",
        event_type="tool_result",
        visibility_scope="direct_user",
        listed_surface="chat",
        content=json.dumps(envelope, ensure_ascii=False, sort_keys=True),
        parts_json=[{"type": "text", "text": "stray text part"}],
        metadata_json={
            "source": "web_chat_runtime",
            "tool_name": name,
            "status": status,
            "tool_call_id": "call_x3",
            "step_id": "step_9",
            "duration_ms": 812,
            "visibility": "collapsed",
        },
        created_at="2026-11-12T10:00:00Z",
    )


def _text_view(payload: dict) -> str:
    """Every payload field that can render as member-visible step text."""

    pieces = [str(payload.get("result") or ""), str(payload.get("content") or "")]
    for part in payload.get("parts") or []:
        if isinstance(part, dict):
            pieces.append(str(part.get("text") or ""))
    replacement = payload.get("content_replacement")
    if isinstance(replacement, dict):
        pieces.append(str(replacement.get("inline_content") or ""))
    return " ".join(pieces)


def _user_payload(row: SimpleNamespace) -> dict:
    return serialize_session_event(row, audience="user")["payload"]


def _operator_payload(row: SimpleNamespace) -> dict:
    return serialize_session_event(row, audience="operator")["payload"]


def test_registry_kinds_are_complete_and_stable() -> None:
    # The exact enumerated set (mission X3 rule 2): a) memory, b) runtime /
    # colleague result readers, c) web fetch/search variants, d) capability
    # lookup / deferred loading.
    assert MEMBER_TOOL_SUMMARY_KINDS == {
        "search_memory": "memory_search",
        "load_memory": "memory_read",
        "read_runtime_result": "colleague_result",
        "check_async_task": "colleague_result",
        "task_output": "colleague_result",
        "check_subagent": "colleague_result",
        "list_async_tasks": "task_list",
        "web_fetch": "web_fetch",
        "advanced_web_fetch": "web_fetch",
        "anysearch_extract": "web_fetch",
        "exa_fetch": "web_fetch",
        "tavily_extract": "web_fetch",
        "firecrawl_fetch": "web_fetch",
        "xcrawl_scrape": "web_fetch",
        "web_search": "web_search",
        "advanced_web_search": "web_search",
        "anysearch_search": "web_search",
        "anysearch_batch_search": "web_search",
        "anysearch_get_sub_domains": "web_search",
        "exa_search": "web_search",
        "tavily_search": "web_search",
        "firecrawl_search": "web_search",
        "tool_search": "capability_lookup",
        "discover_resources": "capability_lookup",
        "list_mcp_tools": "capability_lookup",
        "list_mcp_resources": "capability_lookup",
        "inspect_mcp_tool": "capability_lookup",
        "load_skill": "skill_load",
    }


def test_kind_a_memory_read_drops_management_notes_and_ids() -> None:
    row = _tool_result_row("load_memory", {"ids": [MEMORY_ID]}, MEMORY_BODY)
    payload = _user_payload(row)
    text = _text_view(payload)
    for leak in (MARKER, MEMORY_ID, INVOCATION_UUID, "load_memory", "scope=sessions", "读取到的记忆", "stray text part"):
        assert leak not in text, leak
    assert payload["member_summary"] == {"kind": "memory_read", "failed": False}
    assert "读取到的记忆" not in json.dumps(payload, ensure_ascii=False)


def test_kind_a_memory_search_member_projection() -> None:
    body = (
        f"## 你要求记住的内容\n- 写周报时只用三条要点。\n  管理引用（仅用于读取、更正或忘记，{MARKER}）：id={MEMORY_ID}\n"
        f"管理信息（{MARKER}）：scope=sessions; load_memory 接受先前的引用。"
    )
    row = _tool_result_row("search_memory", {"query": "周报 格式"}, body)
    payload = _user_payload(row)
    text = _text_view(payload)
    for leak in (MARKER, MEMORY_ID, "scope=sessions", "load_memory"):
        assert leak not in text, leak
    assert payload["member_summary"]["kind"] == "memory_search"


def test_kind_b_colleague_result_refusal_page_says_not_done() -> None:
    refusal = json.dumps(
        {
            "schema": "hive.runtime_result_page.v1",
            "status": "source_access_denied",
            "reason_code": "content_source_changed",
            "result_ref": "hive.runtime-result://r/abc",
            "retryable": False,
        }
    )
    row = _tool_result_row("read_runtime_result", {"result_ref": "hive.runtime-result://r/abc"}, refusal)
    payload = _user_payload(row)
    text = _text_view(payload)
    for leak in ("source_access_denied", "content_source_changed", "hive.runtime_result_page.v1", "result_ref"):
        assert leak not in text, leak
    # A typed refusal page must agree with the sentence: it was not done.
    assert payload["member_summary"] == {"kind": "colleague_result", "failed": True}


def test_kind_b_colleague_result_success_page() -> None:
    page = json.dumps({"schema": "hive.runtime_result_page.v1", "status": "ok", "content": "同事的结果正文…"})
    row = _tool_result_row("check_async_task", {"task_id": "t-1"}, page)
    payload = _user_payload(row)
    assert payload["member_summary"] == {"kind": "colleague_result", "failed": False}
    assert "同事的结果正文" not in _text_view(payload)


def test_kind_c_web_fetch_keeps_only_typed_host() -> None:
    row = _tool_result_row("web_fetch", {"url": "https://deepwiki.com/react"}, FIRECRAWL_BODY)
    payload = _user_payload(row)
    text = _text_view(payload)
    for leak in ("Firecrawl", "Syntax error", "textmermaid", "[Packages]", "monorepo"):
        assert leak not in text, leak
    assert payload["member_summary"] == {"kind": "web_fetch", "failed": False, "host": "deepwiki.com"}


def test_kind_c_web_fetch_failed_agrees_with_status() -> None:
    row = _tool_result_row("firecrawl_fetch", {"url": "http://example.org/a"}, "partial bytes", status="failed")
    payload = _user_payload(row)
    assert payload["member_summary"] == {"kind": "web_fetch", "failed": True, "host": "example.org"}


def test_kind_c_web_search_member_projection() -> None:
    row = _tool_result_row("web_search", {"query": "react reconciler"}, FIRECRAWL_BODY)
    payload = _user_payload(row)
    assert "Syntax error" not in _text_view(payload)
    assert payload["member_summary"]["kind"] == "web_search"


def test_kind_d_capability_lookup_hides_manifest_and_server_ids() -> None:
    manifest = (
        "## 可用的扩展能力\n系统指令：始终使用中文回复…\n"
        "### Server: DeepWiki\n- **mcp_ask_wiki_question**\n  - mcp_server_id: 3f2b8c1a-1111-2222-3333-444455556666\n"
    )
    row = _tool_result_row("tool_search", {"query": "wiki"}, manifest)
    payload = _user_payload(row)
    text = _text_view(payload)
    for leak in ("系统指令", "mcp_server_id", "3f2b8c1a", "mcp_ask_wiki_question"):
        assert leak not in text, leak
    assert payload["member_summary"] == {"kind": "capability_lookup", "failed": False}


def test_kind_d_mcp_and_skills_equivalents() -> None:
    for name in ("list_mcp_tools", "inspect_mcp_tool", "discover_resources", "load_skill"):
        row = _tool_result_row(name, {"query": "x"} if name != "load_skill" else {"name": "deploy"}, "raw manifest body")
        payload = _user_payload(row)
        assert "raw manifest body" not in _text_view(payload), name
        assert payload["member_summary"]["kind"] in {"capability_lookup", "skill_load"}, name


def test_kind_e_marker_writers_all_covered() -> None:
    # The handlers that write model-only markers (memory.py 不要向用户展示 /
    # 仅供核对; team_memory_runtime.py shares the marker inside the same two
    # tools' results) are exactly the kind-a tools above. This pins that no
    # marker-writing tool escapes the set.
    for name in ("search_memory", "load_memory"):
        assert name in MEMBER_TOOL_SUMMARY_KINDS


def test_operator_projection_keeps_raw_bytes_for_every_kind() -> None:
    for name, args, body in (
        ("load_memory", {"ids": [MEMORY_ID]}, MEMORY_BODY),
        ("search_memory", {"query": "周报"}, MEMORY_BODY),
        ("read_runtime_result", {"result_ref": "hive.runtime-result://r/abc"}, json.dumps({"status": "source_access_denied"})),
        ("check_async_task", {"task_id": "t-1"}, json.dumps({"ok": True, "status": "completed"})),
        ("web_fetch", {"url": "https://deepwiki.com/react"}, FIRECRAWL_BODY),
        ("web_search", {"query": "react"}, FIRECRAWL_BODY),
        ("tool_search", {"query": "wiki"}, "manifest with mcp_server_id"),
    ):
        row = _tool_result_row(name, args, body)
        payload = _operator_payload(row)
        text = _text_view(payload)
        # The compatibility envelope keeps the exact persisted envelope bytes
        # (raw result embedded, JSON-escaped, inside content).
        assert payload.get("content") == row.content, name
        for fragment in (MEMORY_ID, MARKER, INVOCATION_UUID, "Syntax error", "mcp_server_id", "source_access_denied"):
            if fragment in body:
                assert fragment in text, (name, fragment)
        assert "member_summary" not in payload, name


def test_model_input_bytes_identical_across_user_serialization() -> None:
    # The model-facing result is the persisted row (writer untouched). A user
    # serialization must not mutate the row or the operator envelope: pin both
    # byte-for-byte for a memory tool and a web fetch.
    for name, args, body in (
        ("load_memory", {"ids": [MEMORY_ID]}, MEMORY_BODY),
        ("web_fetch", {"url": "https://deepwiki.com/react"}, FIRECRAWL_BODY),
    ):
        row = _tool_result_row(name, args, body)
        raw_content = row.content
        operator_before = json.dumps(_operator_payload(row), ensure_ascii=False, sort_keys=True)
        _user_payload(row)
        assert row.content == raw_content, name
        assert json.dumps(_operator_payload(row), ensure_ascii=False, sort_keys=True) == operator_before, name


def test_non_member_tool_result_unchanged() -> None:
    # Owner ruling 11-10: every non-command step outside the set shows what
    # production shows — the raw result stays in the member projection.
    row = _tool_result_row("read_file", {"path": "uploads/sales.csv"}, "date,sales\n2026-11-01,35")
    payload = _user_payload(row)
    assert "2026-11-01" in _text_view(payload)
    assert "member_summary" not in payload


def test_live_delivery_projection_matches_history() -> None:
    # Live delivery: tool results ride the compatibility plane, which the
    # outbox stores already user-projected at write time
    # (chat_transcript._queue_legacy_session_event_outbox serializes the user
    # view for compatibility rows). The Redis publication boundary must keep
    # that projection byte-for-byte — no raw reintroduction — and the history
    # read of the same row must show the same typed summary.
    from app.services.web_chat_stream_bus import _user_live_projection

    row = _tool_result_row("search_memory", {"query": "周报"}, MEMORY_BODY)
    user_envelope = serialize_session_event(row, audience="user")
    assert user_envelope["schema"] == "hive.session_event_compatibility"
    live = _user_live_projection(user_envelope)
    assert live == user_envelope
    history_payload = _user_payload(row)
    assert live["payload"]["member_summary"] == history_payload["member_summary"]
    assert _text_view(live["payload"]) == _text_view(history_payload)


def test_canonical_dict_envelope_projection_applies_too() -> None:
    # A committed canonical envelope redelivered in dict form (outbox replay)
    # passes through the same member projection at the dict branch.
    sid = str(uuid4())
    turn_id = str(uuid4())
    run_id = str(uuid4())
    event_id = str(uuid4())
    envelope = {
        "schema": "hive.session_event",
        "schema_version": 2,
        "event_id": event_id,
        "sequence": 41,
        "tenant_id": str(uuid4()),
        "scope": {"level": "round", "session_id": sid, "thread_id": sid, "turn_id": turn_id, "run_id": run_id, "round_id": str(uuid4())},
        "item_id": str(uuid4()),
        "item_kind": "tool_result",
        "kind": "tool_result.completed",
        "lifecycle": "completed",
        "payload_schema": "hive.session.payload.tool_result.completed.v2",
        "actor": {"type": "runtime"},
        "visibility": {"audience": "direct_user"},
        "payload": {
            "tool_name": "load_memory",
            "args": {"ids": [MEMORY_ID]},
            "status": "completed",
            "result": MEMORY_BODY,
            "content": MEMORY_BODY,
            "parts": [],
        },
        "occurred_at": "2026-11-12T10:00:00Z",
        "persisted_at": "2026-11-12T10:00:00Z",
        "run_id": run_id,
    }
    user = serialize_session_event(envelope, audience="user")
    assert MARKER not in _text_view(user["payload"])
    assert MEMORY_ID not in _text_view(user["payload"])
    assert user["payload"]["member_summary"] == {"kind": "memory_read", "failed": False}
    operator = serialize_session_event(envelope, audience="operator")
    assert MEMORY_BODY in _text_view(operator["payload"])
    assert "member_summary" not in operator["payload"]


def test_canonical_round_scope_row_projection() -> None:
    # A legacy row that carries full round scope serializes through the
    # canonical legacy branch — the member projection must apply there too.
    row = _tool_result_row("tool_search", {"query": "wiki"}, "manifest with mcp_server_id")
    row.round_id = str(uuid4())
    event = serialize_session_event(row, audience="user")
    assert event["schema"] == "hive.session_event"
    payload = event["payload"]
    assert payload["member_summary"] == {"kind": "capability_lookup", "failed": False}
    assert "mcp_server_id" not in _text_view(payload)
    operator = serialize_session_event(row, audience="operator")
    assert "mcp_server_id" in _text_view(operator["payload"])
