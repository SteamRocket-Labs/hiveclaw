"""X3 — one plain sentence per expanded member step for summarized tools.

The member projection (`serialize_session_event(..., audience="user")`, the
single seam behind the live socket, SSE subscription, hydration, reconcile and
group history) must drop the raw result bytes for the X3 tool set and carry a
typed summary instead. Decided by tool name and typed fields only; the operator
envelope and the persisted row — the model's input — stay byte-identical.

Round 2: the summary carries `state` ∈ done/not_done/in_progress/unknown, host
parsing never raises, native and legacy payload shapes are separate, legacy
envelope mechanics survive the projection, and the projection is idempotent.
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
    "- 写周报时只用三条要点，每条不超过二十个字。\n"
    f"  来源参考（仅供核对，{MARKER}）：\"session:2026-11-13 user instruction [E2E], "
    f"session-tool-invocation:{INVOCATION_UUID}\"\n"
    f"  管理引用（仅用于读取、更正或忘记，{MARKER}）：id={MEMORY_ID}\n"
    "## 可用情况\n目前只读到了部分记忆，暂不可用的部分不能用来下结论。"
)
FIRECRAWL_BODY = (
    "# React Wiki\n[Syntax error in textmermaid version 11.16.1]\n"
    "[Packages](/packages) · [react](/packages/react) · monorepo/Yarn workspaces"
)


def _legacy_row(name: str, args: dict, result: str, *, status: str = "done") -> SimpleNamespace:
    """A persisted V1 tool_result row exactly as web_chat_runtime writes it.

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
        "step_id": "step_9",
        "duration_ms": 812,
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


def _native_row(name: str, result: str, *, outcome: str = "success", args: dict | None = None) -> SimpleNamespace:
    """A native V2 tool_result row as session_tool_runtime writes it."""

    payload: dict[str, Any] = {
        "invocation_id": "inv-x3",
        "provider_request_id": "req-x3",
        "provider_tool_use_id": "ptu-x3",
        "tool_name": name,
        "outcome": outcome,
        "retryable": False,
        "content": result,
        "content_hash": "h" * 8,
        "content_or_error_ref": "receipt:x3",
        "parts": [],
        "model_visible_content": "bounded replay copy",
    }
    if args is not None:
        payload["args"] = args
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
        invocation_id="inv-x3",
        provider_tool_use_id="ptu-x3",
        content_hash="h" * 8,
        created_at="2026-11-13T10:00:00Z",
        metadata_json={
            "actor": {"type": "tool"},
            "visibility": {"audience": "direct_user"},
            "v2_persisted_at": "2026-11-13T10:00:00Z",
            "v2_payload": payload,
        },
    )


def _result_text_view(payload: dict) -> str:
    """Every payload field that carries the tool's raw result bytes.

    Ruling 5 keeps the legacy envelope mechanics (name, args, status,
    duration_ms, step_id) inside content, so the envelope JSON itself is not
    result prose; only its removed ``result``/``inline_content`` slots, the
    native ``content`` body, text parts and replay copies are.
    """

    pieces: list[str] = [str(payload.get("result") or "")]
    content = payload.get("content")
    if isinstance(content, str) and content.strip():
        try:
            envelope = json.loads(content)
        except (TypeError, ValueError):
            envelope = None
        if isinstance(envelope, dict):
            pieces.append(str(envelope.get("result") or ""))
            replacement = envelope.get("content_replacement")
            if isinstance(replacement, dict):
                pieces.append(str(replacement.get("inline_content") or ""))
        else:
            pieces.append(content)
    for part in payload.get("parts") or []:
        if isinstance(part, dict):
            pieces.append(str(part.get("text") or ""))
    replacement = payload.get("content_replacement")
    if isinstance(replacement, dict):
        pieces.append(str(replacement.get("inline_content") or ""))
    pieces.append(str(payload.get("model_visible_content") or ""))
    return " ".join(pieces)


def _user_payload(row: SimpleNamespace) -> dict:
    return serialize_session_event(row, audience="user")["payload"]


def _operator_payload(row: SimpleNamespace) -> dict:
    return serialize_session_event(row, audience="operator")["payload"]


def test_registry_kinds_are_complete_and_stable() -> None:
    # The exact enumerated set (mission X3 rule 2 + round-2 ruling 8):
    # a) memory, b) runtime / colleague result readers, c) web fetch/search
    # variants, d) capability lookup / deferred loading and its MCP protocol
    # and clawhub equivalents, plus the resident context pages.
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
        "mcp_list_resources": "capability_lookup",
        "mcp_list_prompts": "capability_lookup",
        "search_clawhub": "capability_lookup",
        "load_skill": "skill_load",
        "read_context_resource": "context_read",
    }


def test_kind_a_memory_read_drops_management_notes_and_ids() -> None:
    row = _legacy_row("load_memory", {"ids": [MEMORY_ID]}, MEMORY_BODY)
    payload = _user_payload(row)
    text = _result_text_view(payload)
    for leak in (MARKER, MEMORY_ID, INVOCATION_UUID, "load_memory", "scope=sessions", "读取到的记忆", "stray text part"):
        assert leak not in text, leak
    assert payload["member_summary"] == {"kind": "memory_read", "state": "done"}
    assert json.loads(payload["content"]).get("result") is None


def test_kind_a_memory_search_member_projection() -> None:
    body = (
        f"## 你要求记住的内容\n- 写周报时只用三条要点。\n  管理引用（仅用于读取、更正或忘记，{MARKER}）：id={MEMORY_ID}\n"
        f"管理信息（{MARKER}）：scope=sessions; load_memory 接受先前的引用。"
    )
    row = _legacy_row("search_memory", {"query": "周报 格式"}, body)
    payload = _user_payload(row)
    text = _result_text_view(payload)
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
    row = _legacy_row("read_runtime_result", {"result_ref": "hive.runtime-result://r/abc"}, refusal)
    payload = _user_payload(row)
    text = _result_text_view(payload)
    for leak in ("source_access_denied", "content_source_changed", "hive.runtime_result_page.v1", "result_ref"):
        assert leak not in text, leak
    # A typed refusal page must agree with the sentence: it was not done.
    assert payload["member_summary"] == {"kind": "colleague_result", "state": "not_done"}


def test_kind_b_colleague_result_success_page() -> None:
    page = json.dumps({"schema": "hive.runtime_result_page.v1", "status": "ok", "content": "同事的结果正文…"})
    row = _legacy_row("check_async_task", {"task_id": "t-1"}, page)
    payload = _user_payload(row)
    assert payload["member_summary"] == {"kind": "colleague_result", "state": "done"}
    assert "同事的结果正文" not in _result_text_view(payload)


def test_kind_b_colleague_still_working_is_not_a_read() -> None:
    # R15b: a colleague task still running is not a read result. The typed
    # status of the structured page decides (ruling 2).
    running = json.dumps({"task_id": "t-1", "task_type": "delegation", "status": "running", "result": None})
    payload = _user_payload(_legacy_row("check_async_task", {"task_id": "t-1"}, running))
    assert payload["member_summary"] == {"kind": "colleague_result", "state": "in_progress"}
    queued = json.dumps({"task_id": "t-1", "task_type": "delegation", "status": "queued", "result": None})
    assert _user_payload(_legacy_row("check_async_task", {"task_id": "t-1"}, queued))["member_summary"]["state"] == "in_progress"


def test_kind_b_task_output_running_task_is_in_progress() -> None:
    # task_output's typed status rides on task.status (ruling 2).
    page = json.dumps({"ok": True, "task": {"task_type": "delegation", "status": "running"}})
    payload = _user_payload(_legacy_row("task_output", {"runtime_task_id": "r-1"}, page))
    assert payload["member_summary"] == {"kind": "colleague_result", "state": "in_progress"}


def test_kind_c_web_fetch_keeps_only_typed_host() -> None:
    row = _legacy_row("web_fetch", {"url": "https://deepwiki.com/react"}, FIRECRAWL_BODY)
    payload = _user_payload(row)
    text = _result_text_view(payload)
    for leak in ("Firecrawl", "Syntax error", "textmermaid", "[Packages]", "monorepo"):
        assert leak not in text, leak
    assert payload["member_summary"] == {"kind": "web_fetch", "state": "done", "host": "deepwiki.com"}


def test_kind_c_web_fetch_failed_agrees_with_status() -> None:
    row = _legacy_row("firecrawl_fetch", {"url": "http://example.org/a"}, "partial bytes", status="failed")
    payload = _user_payload(row)
    assert payload["member_summary"] == {"kind": "web_fetch", "state": "not_done", "host": "example.org"}


def test_kind_c_scheme_less_urls_never_raise_and_give_no_host() -> None:
    # Ruling 1: urlparse returns a None hostname for scheme-less URLs, bare
    # schemes and non-host schemes; the member history must not 500 and the
    # sentence simply carries no host.
    for url in ("deepwiki.com/react", "http://", "mailto:a@b.c"):
        row = _legacy_row("web_fetch", {"url": url}, FIRECRAWL_BODY)
        payload = _user_payload(row)
        assert payload["member_summary"] == {"kind": "web_fetch", "state": "done"}, url
        assert "host" not in payload["member_summary"], url


def test_kind_c_web_search_member_projection() -> None:
    row = _legacy_row("web_search", {"query": "react reconciler"}, FIRECRAWL_BODY)
    payload = _user_payload(row)
    assert "Syntax error" not in _result_text_view(payload)
    assert payload["member_summary"]["kind"] == "web_search"


def test_kind_d_capability_lookup_hides_manifest_and_server_ids() -> None:
    manifest = (
        "## 可用的扩展能力\n系统指令：始终使用中文回复…\n"
        "### Server: DeepWiki\n- **mcp_ask_wiki_question**\n  - mcp_server_id: 3f2b8c1a-1111-2222-3333-444455556666\n"
    )
    row = _legacy_row("tool_search", {"query": "wiki"}, manifest)
    payload = _user_payload(row)
    text = _result_text_view(payload)
    for leak in ("系统指令", "mcp_server_id", "3f2b8c1a", "mcp_ask_wiki_question"):
        assert leak not in text, leak
    assert payload["member_summary"] == {"kind": "capability_lookup", "state": "done"}


def test_kind_d_mcp_and_skills_equivalents() -> None:
    for name, args in (
        ("list_mcp_tools", {"query": "x"}),
        ("inspect_mcp_tool", {"query": "x"}),
        ("discover_resources", {"query": "x"}),
        ("mcp_list_resources", {}),
        ("mcp_list_prompts", {}),
        ("search_clawhub", {"query": "x"}),
        ("load_skill", {"name": "deploy"}),
    ):
        row = _legacy_row(name, args, "raw manifest body")
        payload = _user_payload(row)
        assert "raw manifest body" not in _result_text_view(payload), name
        assert payload["member_summary"]["kind"] in {"capability_lookup", "skill_load"}, name


def test_kind_context_read_summarizes_resident_prompt_pages() -> None:
    # Ruling 8: read_context_resource reads resident prompt pages (soul,
    # company information, organization) — the same class as run 9's leak.
    body = "# Soul\nYou are HiveClaw's assistant. 系统指令：始终使用中文回复。"
    row = _legacy_row("read_context_resource", {"resource": "soul"}, body)
    payload = _user_payload(row)
    assert "系统指令" not in _result_text_view(payload)
    assert payload["member_summary"] == {"kind": "context_read", "state": "done"}


def test_kind_e_marker_writers_all_covered() -> None:
    # The handlers that write model-only markers (memory.py 不要向用户展示 /
    # 仅供核对; team_memory_runtime.py shares the marker inside the same two
    # tools' results) are exactly the kind-a tools above. This pins that no
    # marker-writing tool escapes the set.
    for name in ("search_memory", "load_memory"):
        assert name in MEMBER_TOOL_SUMMARY_KINDS


def test_not_done_outcomes_agree_with_the_sentence() -> None:
    # Ruling 3: every outcome that is not success says not done. Native
    # settlements record denied / aborted / cancelled / unavailable /
    # approval_required; structured pages add stale_resource and forbidden.
    cases = [
        (_native_row("web_fetch", '{"reason_code":"earlier_tool_waiting_for_session_permission","status":"aborted","retryable":true}', outcome="aborted"), "aborted"),
        (_native_row("web_fetch", "Blocked by hook: policy", outcome="denied"), "denied"),
        (_native_row("web_fetch", "cancelled", outcome="cancelled"), "cancelled"),
        (_native_row("read_runtime_result", json.dumps({"status": "stale_resource", "expected_sha256": "a", "actual_sha256": "b"})), "stale_resource"),
        (_native_row("check_async_task", json.dumps({"status": "forbidden"})), "forbidden"),
    ]
    for row, label in cases:
        summary = _user_payload(row)["member_summary"]
        assert summary["state"] == "not_done", (label, summary)


def test_unknown_outcome_renders_the_unconfirmed_state() -> None:
    row = _native_row("web_fetch", "delivery unknown", outcome="unknown")
    assert _user_payload(row)["member_summary"] == {"kind": "web_fetch", "state": "unknown"}


def test_native_ok_false_is_never_masked_by_inner_result() -> None:
    # Ruling 4: the top-level typed ok:false decides even when an inner
    # result string exists.
    row = _native_row("check_async_task", json.dumps({"ok": False, "status": "lost", "result": "colleague crashed"}))
    assert _user_payload(row)["member_summary"]["state"] == "not_done"


def test_old_native_row_without_tool_name_stays_unsummarized() -> None:
    # Ruling 4: rows written before the tool_name key keep their bytes; the
    # kind is never mined out of the result body (rule 1). Controller
    # decision: pre-deploy rows are lab data and are not covered.
    row = _native_row("read_file", json.dumps({"name": "web_search", "version": "1.0.0"}))
    row.metadata_json["v2_payload"] = {
        "invocation_id": "inv-x3",
        "outcome": "success",
        "content": json.dumps({"name": "web_search", "version": "1.0.0"}),
        "parts": [],
    }
    payload = _user_payload(row)
    assert "member_summary" not in payload
    assert json.loads(payload["content"]) == {"name": "web_search", "version": "1.0.0"}


def test_legacy_row_keeps_envelope_mechanics() -> None:
    # Ruling 5: the embedded envelope survives minus its raw result — name,
    # args, status, duration_ms and step_id re-serialize into content so the
    # member view keeps the failed status, duration and pairing mechanics.
    row = _legacy_row("web_fetch", {"url": "https://deepwiki.com/react"}, FIRECRAWL_BODY, status="failed")
    payload = _user_payload(row)
    envelope = json.loads(payload["content"])
    assert envelope["name"] == "web_fetch"
    assert envelope["status"] == "failed"
    assert envelope["duration_ms"] == 812
    assert envelope["step_id"] == "step_9"
    assert "result" not in envelope
    assert FIRECRAWL_BODY not in _result_text_view(payload)
    assert payload["member_summary"] == {"kind": "web_fetch", "state": "not_done", "host": "deepwiki.com"}


def test_operator_projection_keeps_raw_bytes_for_every_kind() -> None:
    for row in (
        _legacy_row("load_memory", {"ids": [MEMORY_ID]}, MEMORY_BODY),
        _legacy_row("search_memory", {"query": "周报"}, MEMORY_BODY),
        _legacy_row("read_runtime_result", {"result_ref": "hive.runtime-result://r/abc"}, json.dumps({"status": "source_access_denied"})),
        _legacy_row("check_async_task", {"task_id": "t-1"}, json.dumps({"ok": True, "status": "completed"})),
        _legacy_row("web_fetch", {"url": "https://deepwiki.com/react"}, FIRECRAWL_BODY),
        _legacy_row("web_search", {"query": "react"}, FIRECRAWL_BODY),
        _legacy_row("tool_search", {"query": "wiki"}, "manifest with mcp_server_id"),
        _native_row("load_memory", MEMORY_BODY),
        _native_row("web_fetch", FIRECRAWL_BODY),
    ):
        name = row.metadata_json["v2_payload"]["tool_name"] if hasattr(row, "scope_json") else row.metadata_json["tool_name"]
        payload = _operator_payload(row)
        text = _result_text_view(payload)
        # The compatibility envelope keeps the exact persisted envelope bytes
        # (raw result embedded, JSON-escaped, inside content); the operator
        # view must keep every leak string the member view removes.
        assert payload.get("content") == row.content if hasattr(row, "content") else True, name
        for fragment in (MEMORY_ID, MARKER, INVOCATION_UUID, "Syntax error", "mcp_server_id", "source_access_denied", "bounded replay copy"):
            if hasattr(row, "content") and fragment in row.content:
                assert fragment in text, (name, fragment)
        assert "member_summary" not in payload, name


def test_model_input_bytes_identical_across_user_serialization() -> None:
    # The model-facing result is the persisted row (writer untouched). A user
    # serialization must not mutate the row or the operator envelope: pin both
    # byte-for-byte for a memory tool and a web fetch.
    for row in (
        _legacy_row("load_memory", {"ids": [MEMORY_ID]}, MEMORY_BODY),
        _legacy_row("web_fetch", {"url": "https://deepwiki.com/react"}, FIRECRAWL_BODY),
    ):
        raw_content = row.content
        operator_before = json.dumps(_operator_payload(row), ensure_ascii=False, sort_keys=True)
        _user_payload(row)
        assert row.content == raw_content, row.metadata_json["tool_name"]
        assert json.dumps(_operator_payload(row), ensure_ascii=False, sort_keys=True) == operator_before


def test_non_member_tool_result_unchanged() -> None:
    # Owner ruling 11-10: every non-command step outside the set shows what
    # production shows — the raw result stays in the member projection.
    row = _legacy_row("read_file", {"path": "uploads/sales.csv"}, "date,sales\n2026-11-01,35")
    payload = _user_payload(row)
    assert "2026-11-01" in _result_text_view(payload)
    assert "member_summary" not in payload


def test_projection_is_idempotent() -> None:
    # Ruling 6: projecting an already-projected payload returns it as is, so
    # redelivery from either plane carries the same bytes.
    from app.services.session_event_contract import _apply_member_tool_summary

    row = _legacy_row("load_memory", {"ids": [MEMORY_ID]}, MEMORY_BODY)
    once = _user_payload(row)
    twice = _apply_member_tool_summary(dict(once), item_kind="tool_result")
    assert twice == once


def test_live_delivery_projection_matches_history() -> None:
    # Live delivery: tool results ride the compatibility plane, which the
    # outbox stores already user-projected at write time. The Redis
    # publication boundary must keep that projection byte-for-byte — no raw
    # reintroduction — and the history read of the same row must show the
    # same typed summary.
    from app.services.web_chat_stream_bus import _user_live_projection

    row = _legacy_row("search_memory", {"query": "周报"}, MEMORY_BODY)
    user_envelope = serialize_session_event(row, audience="user")
    assert user_envelope["schema"] == "hive.session_event_compatibility"
    live = _user_live_projection(user_envelope)
    assert live == user_envelope
    history_payload = _user_payload(row)
    assert live["payload"]["member_summary"] == history_payload["member_summary"]
    assert _result_text_view(live["payload"]) == _result_text_view(history_payload)


def test_canonical_dict_envelope_projection_applies_too() -> None:
    # A committed canonical envelope redelivered in dict form (outbox replay)
    # passes through the same member projection at the dict branch.
    sid = str(uuid4())
    turn_id = str(uuid4())
    run_id = str(uuid4())
    envelope = {
        "schema": "hive.session_event",
        "schema_version": 2,
        "event_id": str(uuid4()),
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
            "invocation_id": "inv-1",
            "outcome": "success",
            "result": MEMORY_BODY,
            "content": MEMORY_BODY,
            "parts": [],
        },
        "occurred_at": "2026-11-12T10:00:00Z",
        "persisted_at": "2026-11-12T10:00:00Z",
        "run_id": run_id,
    }
    user = serialize_session_event(envelope, audience="user")
    assert MARKER not in _result_text_view(user["payload"])
    assert MEMORY_ID not in _result_text_view(user["payload"])
    assert user["payload"]["member_summary"] == {"kind": "memory_read", "state": "done"}
    operator = serialize_session_event(envelope, audience="operator")
    assert MEMORY_BODY in _result_text_view(operator["payload"])
    assert "member_summary" not in operator["payload"]


def test_native_v2_row_user_and_operator_projection() -> None:
    # Ruling 7: a real native V2 row (the current web runtime's write path).
    row = _native_row("load_memory", MEMORY_BODY)
    user = _user_payload(row)
    assert user["member_summary"] == {"kind": "memory_read", "state": "done"}
    text = _result_text_view(user)
    for leak in (MARKER, MEMORY_ID, INVOCATION_UUID, "bounded replay copy"):
        assert leak not in text, leak
    assert user.get("model_visible_content") == ""
    operator = _operator_payload(row)
    assert MARKER in operator["content"]
    assert operator.get("model_visible_content") == "bounded replay copy"
    assert "member_summary" not in operator
