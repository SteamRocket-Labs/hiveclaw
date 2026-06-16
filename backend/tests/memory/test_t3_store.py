"""Governed T3 append API tests (docs/agent-memory-md-first-spec.md §12 P2).

Acceptance:
- Heartbeat cannot bypass write gate for T3 (single governed append path).
- Entries have ids and lifecycle records.
- Hindsight sync remains derived and best effort.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from app.memory.t3_store import T3AppendResult, append_t3_memory_candidate


@pytest.mark.asyncio
async def test_append_t3_memory_candidate_uses_llm_primary_write_gate(tmp_path: Path, monkeypatch) -> None:
    from app.memory import t3_store
    from app.memory.write_gate import MemoryWriteDecision

    tenant_id = uuid.uuid4()
    calls = []

    async def fake_gate(content: str, **kwargs):
        calls.append(kwargs)
        return MemoryWriteDecision(
            original_content=content,
            content=content,
            category=kwargs["category"],
            sensitivity="PL1_public",
            metadata={
                "entry_id": "llm-t3-entry",
                "sensitivity": "PL1_public",
                "status": "active",
                "version": "1",
                "threat_gate_method": "llm_classifier",
            },
        )

    monkeypatch.setattr(t3_store, "prepare_memory_write_with_llm", fake_gate)

    result = await append_t3_memory_candidate(
        uuid.uuid4(),
        category="feedback",
        content="User prefers concise output",
        proposed_by="heartbeat",
        tenant_id=tenant_id,
        data_root=tmp_path,
    )

    assert result.status == "accepted"
    assert calls and calls[0]["tenant_id"] == tenant_id


@pytest.mark.asyncio
async def test_append_accepted_entry_has_id_lifecycle_and_index(tmp_path: Path) -> None:
    agent_id = uuid.uuid4()

    result = await append_t3_memory_candidate(
        agent_id,
        category="feedback",
        content="User requires plain text responses — no emoji",
        source_refs=["t2:learnings/insights.md#entry:abc"],
        evidence="user_stated",
        confidence=0.9,
        proposed_by="heartbeat",
        data_root=tmp_path,
    )

    assert isinstance(result, T3AppendResult)
    assert result.status == "accepted"
    assert result.entry_id

    # Entry landed in the right T3 file. D2: prose carries only [date][entry_id];
    # proposed_by and every other field live in the lifecycle sidecar.
    body = (tmp_path / str(agent_id) / "memory" / "feedback.md").read_text(encoding="utf-8")
    assert "User requires plain text responses" in body
    assert f"[entry_id={result.entry_id}]" in body
    assert "[proposed_by=" not in body
    assert "[sensitivity=" not in body

    # Lifecycle record exists for the entry id and carries the migrated metadata.
    lifecycle = json.loads((tmp_path / str(agent_id) / "memory" / "lifecycle.json").read_text(encoding="utf-8"))
    record = next(r for r in lifecycle if r["id"] == result.entry_id)
    assert record["status"] == "active"
    assert record["metadata"].get("proposed_by") == "heartbeat"

    # INDEX.md was rebuilt and lists the entry.
    index_body = (tmp_path / str(agent_id) / "memory" / "INDEX.md").read_text(encoding="utf-8")
    assert result.entry_id in index_body


@pytest.mark.asyncio
async def test_append_rejects_pl4_credentials(tmp_path: Path) -> None:
    agent_id = uuid.uuid4()

    result = await append_t3_memory_candidate(
        agent_id,
        category="reference",
        content="Production API key is sk-live-abcdef1234567890abcdef",
        proposed_by="heartbeat",
        data_root=tmp_path,
    )

    assert result.status == "rejected"
    assert result.reason
    body_path = tmp_path / str(agent_id) / "memory" / "knowledge.md"
    if body_path.exists():
        assert "sk-live-abcdef1234567890abcdef" not in body_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_append_skips_near_duplicate(tmp_path: Path) -> None:
    agent_id = uuid.uuid4()

    first = await append_t3_memory_candidate(
        agent_id,
        category="feedback",
        content="User prefers short replies in chat",
        proposed_by="heartbeat",
        data_root=tmp_path,
    )
    assert first.status == "accepted"

    second = await append_t3_memory_candidate(
        agent_id,
        category="feedback",
        content="The user prefers short replies in chat conversations",
        proposed_by="heartbeat",
        data_root=tmp_path,
    )
    assert second.status == "duplicate"
    assert second.similar is not None

    body = (tmp_path / str(agent_id) / "memory" / "feedback.md").read_text(encoding="utf-8")
    assert body.count("prefers short replies") == 1


@pytest.mark.asyncio
async def test_duplicate_append_reinforces_existing_entry_counters(tmp_path: Path) -> None:
    agent_id = uuid.uuid4()

    first = await append_t3_memory_candidate(
        agent_id,
        category="feedback",
        content="User prefers concise deployment summaries",
        evidence="user_stated",
        proposed_by="heartbeat",
        data_root=tmp_path,
    )
    second = await append_t3_memory_candidate(
        agent_id,
        category="feedback",
        content="The user prefers concise deployment summaries.",
        evidence="user_stated",
        proposed_by="heartbeat",
        data_root=tmp_path,
    )

    assert second.status == "duplicate"
    assert second.entry_id == first.entry_id
    assert second.similar is not None
    assert second.similar["counter_delta"]["reinforcement_count"] == "2"
    assert second.similar["counter_delta"]["helpful_count"] == "2"

    lifecycle = json.loads((tmp_path / str(agent_id) / "memory" / "lifecycle.json").read_text(encoding="utf-8"))
    record = next(r for r in lifecycle if r["id"] == first.entry_id)
    assert record["metadata"]["memory_signature"]
    assert record["metadata"]["reinforcement_count"] == "2"
    assert record["metadata"]["helpful_count"] == "2"
    assert record["metadata"]["harmful_count"] == "0"

    body = (tmp_path / str(agent_id) / "memory" / "feedback.md").read_text(encoding="utf-8")
    assert body.count("concise deployment summaries") == 1


@pytest.mark.asyncio
async def test_duplicate_append_tracks_harmful_counter_without_new_prose(tmp_path: Path) -> None:
    agent_id = uuid.uuid4()

    first = await append_t3_memory_candidate(
        agent_id,
        category="strategy",
        content="Use cached vendor data before running a full crawl",
        evidence="system_observed",
        proposed_by="heartbeat",
        data_root=tmp_path,
    )
    second = await append_t3_memory_candidate(
        agent_id,
        category="strategy",
        content="Use cached vendor data before doing a full crawl.",
        evidence="misleading",
        proposed_by="feedback",
        data_root=tmp_path,
    )

    assert second.status == "duplicate"
    assert second.entry_id == first.entry_id

    lifecycle = json.loads((tmp_path / str(agent_id) / "memory" / "lifecycle.json").read_text(encoding="utf-8"))
    record = next(r for r in lifecycle if r["id"] == first.entry_id)
    assert record["metadata"]["reinforcement_count"] == "2"
    assert record["metadata"]["helpful_count"] == "1"
    assert record["metadata"]["harmful_count"] == "1"

    body = (tmp_path / str(agent_id) / "memory" / "strategies.md").read_text(encoding="utf-8")
    assert body.count("cached vendor data") == 1


@pytest.mark.asyncio
async def test_append_preserves_container_candidate_marker(tmp_path: Path) -> None:
    agent_id = uuid.uuid4()

    result = await append_t3_memory_candidate(
        agent_id,
        category="strategy",
        content="Research → design → verify three-phase workflow reduces review iterations",
        source_refs=["t2:learnings/insights.md#entry:xyz"],
        container_candidate="skill_candidate",
        proposed_by="heartbeat",
        data_root=tmp_path,
    )

    assert result.status == "accepted"
    # D2: container marker lives in the sidecar metadata, not inlined in prose.
    body = (tmp_path / str(agent_id) / "memory" / "strategies.md").read_text(encoding="utf-8")
    assert "[container=" not in body
    lifecycle = json.loads((tmp_path / str(agent_id) / "memory" / "lifecycle.json").read_text(encoding="utf-8"))
    record = next(r for r in lifecycle if r["id"] == result.entry_id)
    assert record["metadata"].get("container") == "skill_candidate"


@pytest.mark.asyncio
async def test_hindsight_sync_failure_is_non_fatal(tmp_path: Path, monkeypatch) -> None:
    agent_id = uuid.uuid4()

    async def boom(*args, **kwargs):
        raise RuntimeError("hindsight down")

    monkeypatch.setattr("app.memory.hindsight_sync.sync_t3_to_hindsight", boom)

    result = await append_t3_memory_candidate(
        agent_id,
        category="reference",
        content="Hindsight outage must not block durable memory writes",
        proposed_by="extractor",
        tenant_id=uuid.uuid4(),
        data_root=tmp_path,
    )

    assert result.status == "accepted"
    body = (tmp_path / str(agent_id) / "memory" / "knowledge.md").read_text(encoding="utf-8")
    assert "Hindsight outage must not block durable memory writes" in body


# ── No-bypass enforcement: raw file writes under memory/ are refused ──


def test_write_file_tool_refuses_t3_memory_paths(tmp_path: Path) -> None:
    from app.services.agent_tool_domains.workspace import _write_file

    ws = tmp_path / "ws"
    (ws / "memory").mkdir(parents=True)

    for rel in ("memory/feedback.md", "memory/knowledge.md", "memory/learnings/insights.md"):
        out = _write_file(ws, rel, "- [2026-06-04] smuggled entry")
        assert "save_memory" in out
        assert "[Error]" in out or "❌" in out or "denied" in out.lower() or "governed" in out.lower()
        assert not (ws / rel).exists() or "smuggled" not in (ws / rel).read_text(encoding="utf-8", errors="replace")


def test_edit_file_tool_refuses_t3_memory_paths(tmp_path: Path) -> None:
    from app.services.agent_tool_domains.workspace import _edit_file

    ws = tmp_path / "ws"
    (ws / "memory").mkdir(parents=True)
    target = ws / "memory" / "feedback.md"
    target.write_text("# Feedback\n\n- [2026-06-01] original entry\n", encoding="utf-8")

    out = _edit_file(ws, "memory/feedback.md", old_text="original entry", new_text="tampered entry")
    assert "save_memory" in out
    assert "tampered" not in target.read_text(encoding="utf-8")


def test_file_tools_refuse_platform_managed_system_paths(tmp_path: Path) -> None:
    from app.services.agent_tool_domains.workspace import _delete_file, _edit_file, _write_file

    ws = tmp_path / "ws"
    ws.mkdir()

    for rel in ("logs/session.md", "evolution/skill_candidates/proposal.md"):
        out = _write_file(ws, rel, "manual bypass")
        assert "managed by platform services" in out
        assert not (ws / rel).exists()

    for rel in ("logs/session.md", "evolution/skill_candidates/proposal.md", "memory/feedback.md"):
        target = ws / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("platform owned content", encoding="utf-8")

        edit_out = _edit_file(ws, rel, old_text="platform owned", new_text="manual bypass")
        assert "managed by platform services" in edit_out
        assert "manual bypass" not in target.read_text(encoding="utf-8")

        delete_out = _delete_file(ws, rel)
        assert "managed by platform services" in delete_out
        assert target.exists()


def test_workspace_files_outside_memory_still_writable(tmp_path: Path) -> None:
    from app.services.agent_tool_domains.workspace import _write_file

    ws = tmp_path / "ws"
    ws.mkdir()
    out = _write_file(ws, "workspace/report.md", "# Report")
    assert "✅" in out
    assert (ws / "workspace" / "report.md").read_text(encoding="utf-8") == "# Report"
