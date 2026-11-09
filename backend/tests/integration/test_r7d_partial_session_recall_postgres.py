"""A damaged primary excludes its session without disabling other authorized recall."""

import asyncio
import hashlib
import json
import threading
import time
import uuid

import pytest

from app.memory.t0 import ledger
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m1g38_session_recall_actor_postgres import _search, recall_results  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")

NOTICE = "部分较早的对话暂时无法读取，相关内容未用于本次回忆。"
GENERAL_NOTICE = "目前只读到了部分记忆，暂不可用的部分不能用来下结论，可以稍后重试。"
CODES = {"oversized": "t0_record_too_large", "body": "t0_record_hash_mismatch", "truncated": "t0_record_truncated"}


async def _paired_sessions(n, owner_sessionmaker, *, healthy_source="ledger", damage="body"):
    # Scan the bad session first, so returning early cannot accidentally pass.
    healthy, damaged = sorted([uuid.uuid4(), uuid.uuid4()])
    async with owner_sessionmaker() as db:
        for session in (healthy, damaged):
            db.add(ChatSession(id=session, tenant_id=n.tenant, agent_id=n.agent, user_id=n.user,
                audience_kind="private"))
        await db.flush()
        db.add(ChatMessage(tenant_id=n.tenant, agent_id=n.agent, user_id=n.user, role="user",
            conversation_id=str(damaged), content="meeting DAMAGED_DB_FALLBACK"))
        if healthy_source == "db":
            db.add(ChatMessage(tenant_id=n.tenant, agent_id=n.agent, user_id=n.user, role="user",
                conversation_id=str(healthy), content="meeting INTACT_CONVERSATION INTACT_CONTINUATION"))
        await db.commit()
    logs = n.root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    for session, marker in [(damaged, "DAMAGED_CHAT_LOG"), (healthy, "INTACT_CONVERSATION INTACT_CONTINUATION")]:
        if session == healthy and healthy_source != "legacy":
            continue
        (logs / f"chat-{session}.md").write_text(f"---\ntype: chat\nsession_id: {session}\nsource: web\n"
            f"started: 2026-11-01T00:00:00Z\n---\n**User**: meeting {marker}\n")
    if healthy_source == "ledger":
        ledger.append_t0_session_event(agent_id=n.agent, session_id=healthy, event_type="user_message", role="user",
            content="meeting INTACT_CONVERSATION\nINTACT_CONTINUATION", source="web", data_root=n.root.parent)
    # A valid matching prefix must also be discarded when a later row is corrupt.
    ledger.append_t0_session_event(agent_id=n.agent, session_id=damaged, event_type="user_message", role="user",
        content="meeting DAMAGED_PRIMARY_PREFIX", source="web", data_root=n.root.parent)
    written = ledger.append_t0_session_event(agent_id=n.agent, session_id=damaged,
        event_type="tool_result.completed", role="tool", content="meeting DAMAGED_PRIMARY_TAIL",
        source="web", data_root=n.root.parent)
    if damage == "truncated":
        written.jsonl_path.write_bytes(written.jsonl_path.read_bytes()[:-8])
    else:
        rows = [json.loads(line) for line in written.jsonl_path.read_text().splitlines()]
        rows[-1]["content"] = "meeting DAMAGED_REWRITE"
        if damage == "oversized":
            rows[-1]["content"] += "x" * ledger._MAX_EVENT_RECORD_BYTES
            rows[-1]["event_hash"] = ledger._record_hash(rows[-1])
            index_path = written.jsonl_path.parents[2] / "index.json"
            index = json.loads(index_path.read_text())
            index["last_event_hash"] = rows[-1]["event_hash"]
            index_path.write_text(json.dumps(index))
        written.jsonl_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return healthy, damaged


def _fingerprints(n):
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (n.root / "memory/t0/sessions").rglob("*") if path.is_file()}


@pytest.mark.parametrize("damage", ["oversized", "body", "truncated"])
@pytest.mark.parametrize("healthy_source", ["ledger", "legacy", "db"])
async def test_native_recall_keeps_readable_hits_and_reports_unreadable_sessions(
    native_workspace, owner_sessionmaker, monkeypatch, recall_results, damage, healthy_source,  # noqa: F811
):
    from app.services import session_recall

    n = native_workspace
    healthy, damaged = await _paired_sessions(n, owner_sessionmaker, healthy_source=healthy_source, damage=damage)
    before = _fingerprints(n)
    secondary_calls = []
    original_logs, original_db = session_recall._search_t0_chat_logs, session_recall._search_session_history_db

    def logs(*args, **kwargs):
        secondary_calls.append(("legacy", set(kwargs["authorized_session_ids"])))
        return original_logs(*args, **kwargs)

    async def database(*args, **kwargs):
        secondary_calls.append(("db", set(kwargs["authorized_session_ids"])))
        return await original_db(*args, **kwargs)

    monkeypatch.setattr(session_recall, "_search_t0_chat_logs", logs)
    monkeypatch.setattr(session_recall, "_search_session_history_db", database)
    result = await _search(n)
    assert recall_results, result
    hits = recall_results[-1]
    assert len(hits) == 1 and hits[0]["session_id"] == str(healthy), hits
    assert "INTACT_CONVERSATION" in hits[0]["transcript"] and "INTACT_CONTINUATION" in hits[0]["transcript"]
    assert "DAMAGED_" not in str(hits) + result and str(damaged) not in str(hits) + result
    assert "<tool_error>" not in result, result
    availability = json.loads(result.split("## 可用情况\n", 1)[1])
    # Keep the general warning: the same result also has unverified recall sources.
    assert availability["status"] == "partial" and availability["message"] == GENERAL_NOTICE + NOTICE
    # R2 aggregates damaged sessions without losing the exact integrity codes.
    assert {"status": "unavailable", "code": "session_recall_evidence_unavailable",
        "count": 1, "integrity_codes": [CODES[damage]]} in availability["issues"]
    assert {"status": "unavailable", "code": "session_recall_sources_unverified", "count": 1} in availability["issues"]
    # Keep R1's source-verification rule, exactly as m1g38 does: ACL-valid
    # service hits are observed inside the real tool, not promoted to model input.
    assert "INTACT_CONVERSATION" not in result
    expected = [] if healthy_source == "ledger" else ["legacy"] if healthy_source == "legacy" else ["legacy", "db"]
    assert secondary_calls == [(source, {str(healthy)}) for source in expected]
    assert _fingerprints(n) == before


@pytest.mark.parametrize("snapshot_failures", [1, 2])
async def test_native_recall_retries_a_changed_snapshot_once(
    native_workspace, owner_sessionmaker, monkeypatch, recall_results, snapshot_failures,  # noqa: F811
):
    from app.services import session_recall

    n = native_workspace
    session = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(ChatSession(id=session, tenant_id=n.tenant, agent_id=n.agent, user_id=n.user, audience_kind="private"))
        await db.commit()
    ledger.append_t0_session_event(agent_id=n.agent, session_id=session, event_type="user_message", role="user",
        content="meeting INTACT_CONVERSATION INTACT_CONTINUATION", source="web", data_root=n.root.parent)
    before = _fingerprints(n)
    calls = []
    original = session_recall.replay_t0_session_events

    def concurrent_append(**kwargs):
        calls.append(kwargs["session_id"])
        if len(calls) <= snapshot_failures:
            raise ledger.T0LedgerIntegrityError("t0_snapshot_changed")
        return original(**kwargs)

    monkeypatch.setattr(session_recall, "replay_t0_session_events", concurrent_append)
    result = await _search(n)
    if snapshot_failures == 1:
        assert "<tool_error>" not in result, result
        assert recall_results and recall_results[-1][0]["session_id"] == str(session)
        assert "INTACT_CONVERSATION" in recall_results[-1][0]["transcript"]
        assert "INTACT_CONTINUATION" in recall_results[-1][0]["transcript"]
        availability = json.loads(result.split("## 可用情况\n", 1)[1])
        assert availability["issues"] == [
            {"status": "unavailable", "code": "session_recall_sources_unverified", "count": 1}]
        assert availability["message"] == GENERAL_NOTICE
    else:
        error = json.loads(result.split("<tool_error>", 1)[1].split("</tool_error>", 1)[0])
        assert error["reason_code"] == "session_recall_evidence_unavailable"
        assert error["integrity_code"] == "t0_snapshot_changed" and error["retryable"] is True
        assert error["ok"] is False and error["http_status"] == 503
        assert recall_results == []
    assert calls == [str(session)] * 2
    assert _fingerprints(n) == before


async def test_native_recall_aggregates_damaged_sessions_without_identifiers(
    native_workspace, owner_sessionmaker, recall_results,  # noqa: F811
):
    n = native_workspace
    pairs = [await _paired_sessions(n, owner_sessionmaker, damage=damage)
        for damage in ("body", "body", "truncated")]
    result = await _search(n)
    assert "<tool_error>" not in result, result
    assert recall_results and recall_results[-1][0]["session_id"] in {str(healthy) for healthy, _ in pairs}
    assert "INTACT_CONVERSATION" in recall_results[-1][0]["transcript"]
    availability = json.loads(result.split("## 可用情况\n", 1)[1])
    damaged_issues = [issue for issue in availability["issues"] if issue["code"] == "session_recall_evidence_unavailable"]
    assert damaged_issues == [{"status": "unavailable", "code": "session_recall_evidence_unavailable",
        "count": 3, "integrity_codes": sorted({CODES["body"], CODES["truncated"]})}]
    assert all(str(damaged) not in result + str(recall_results) for _, damaged in pairs)
    assert "DAMAGED_" not in result + str(recall_results)


async def test_native_recall_skips_invalid_utf8_log_before_healthy_db_hit(
    native_workspace, owner_sessionmaker, recall_results,  # noqa: F811
):
    n = native_workspace
    healthy, damaged = await _paired_sessions(n, owner_sessionmaker, healthy_source="db")
    (n.root / "logs" / f"chat-{damaged}.md").write_bytes(b"\xff")
    result = await _search(n)
    assert "<tool_error>" not in result, result
    assert recall_results and len(recall_results[-1]) == 1
    assert recall_results[-1][0]["session_id"] == str(healthy)
    assert "INTACT_CONVERSATION" in recall_results[-1][0]["transcript"]
    assert "INTACT_CONTINUATION" in recall_results[-1][0]["transcript"]
    assert "DAMAGED_" not in result + str(recall_results) and str(damaged) not in result + str(recall_results)


async def test_native_recall_without_readable_hits_preserves_oversized_error(
    native_workspace, owner_sessionmaker,  # noqa: F811
):
    n = native_workspace
    await _paired_sessions(n, owner_sessionmaker, damage="oversized")
    result = str(await n.service.execute("search_memory", {"query": "no-such-conversation", "scope": "sessions"},
        agent_id=n.agent, user_id=n.user, session_id=str(n.session), runtime_task_id=str(n.run), emit_runtime_hooks=False))
    error = json.loads(result.split("<tool_error>", 1)[1].split("</tool_error>", 1)[0])
    assert error["reason_code"] == "session_recall_evidence_unavailable" and error["integrity_code"] == CODES["oversized"]
    assert error["ok"] is False and error["http_status"] == 503 and error["retryable"] is False
    assert error["outcome"] == "unavailable" and error["error_class"] == "incomplete_content"


async def test_native_large_ledger_scan_allows_same_loop_progress(
    native_workspace, owner_sessionmaker, monkeypatch, recall_results,  # noqa: F811
):
    n = native_workspace
    session = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(ChatSession(id=session, tenant_id=n.tenant, agent_id=n.agent, user_id=n.user, audience_kind="private"))
        await db.commit()
    written = ledger.append_t0_session_event(agent_id=n.agent, session_id=session, event_type="user_message", role="user",
        content="meeting LARGE_LEDGER " + "ordinary " * 600_000, source="web", data_root=n.root.parent)
    assert written.jsonl_path.stat().st_size > 4 * 1024 * 1024
    entered, release = threading.Event(), threading.Event()
    scan_started = []
    original = ledger._parse_events_from_jsonl

    def slow_read(**kwargs):
        # Deterministically model a slow disk at the real parser, then read and
        # verify the actual large ledger. No fake replay or authority is used.
        scan_started.append(time.monotonic())
        entered.set()
        release.wait(1.0)
        return original(**kwargs)

    monkeypatch.setattr(ledger, "_parse_events_from_jsonl", slow_read)

    async def ticker():
        while not entered.is_set():
            await asyncio.sleep(0.005)
        delay = time.monotonic() - scan_started[0]
        release.set()
        return delay

    tick = asyncio.create_task(ticker())
    try:
        result = await _search(n)
        delay = await asyncio.wait_for(tick, timeout=2)
    finally:
        release.set()
        tick.cancel()
        await asyncio.gather(tick, return_exceptions=True)
    assert "<tool_error>" not in result, result
    assert recall_results and "LARGE_LEDGER" in recall_results[-1][0]["transcript"]
    assert delay < 0.25, f"Event-loop ticker stalled for {delay:.3f}s during the ledger scan"
    print(f"R7d ticker advanced {delay:.3f}s after ledger scan entry (bound 0.250s)")
