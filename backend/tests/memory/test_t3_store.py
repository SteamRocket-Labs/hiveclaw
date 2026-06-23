"""Compatibility tests for the retired T3 append API.

`append_t3_memory_candidate()` no longer commits accepted T3. It remains as a
guarded compatibility adapter that writes explicit overlay entries or rejects
unsafe/episodic content.
"""

from __future__ import annotations

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
            metadata={"threat_gate_method": "llm_classifier"},
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

    assert result.status == "overlay"
    assert calls and calls[0]["tenant_id"] == tenant_id


@pytest.mark.asyncio
async def test_append_adapter_writes_overlay_not_accepted_t3(tmp_path: Path) -> None:
    agent_id = uuid.uuid4()

    result = await append_t3_memory_candidate(
        agent_id,
        category="feedback",
        content="User requires plain text responses — no emoji",
        source_refs=["t2:segment/summary.md#entry:abc"],
        evidence="user_stated",
        confidence=0.9,
        proposed_by="heartbeat",
        data_root=tmp_path,
    )

    assert isinstance(result, T3AppendResult)
    assert result.status == "overlay"
    assert result.entry_id

    memory_dir = tmp_path / str(agent_id) / "memory"
    assert (memory_dir / "explicit" / "entries" / f"{result.entry_id}.md").exists()
    assert "User requires plain text responses" in (memory_dir / "explicit" / "MEMORY.md").read_text(encoding="utf-8")
    assert not (memory_dir / "t3" / "user.md").exists()


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
    assert not (tmp_path / str(agent_id) / "memory" / "explicit" / "MEMORY.md").exists()


@pytest.mark.asyncio
async def test_agent_tool_episodic_scan_log_is_refused(tmp_path: Path) -> None:
    agent_id = uuid.uuid4()

    result = await append_t3_memory_candidate(
        agent_id,
        category="strategy",
        content="2026-06-04 17:00 midday_scan: 窗口内 14 个展会无变化",
        proposed_by="agent_tool",
        data_root=tmp_path,
    )

    assert result.status == "episodic"
    assert "workspace" in result.reason.lower() or "session log" in result.reason.lower()
    assert not (tmp_path / str(agent_id) / "memory" / "explicit" / "MEMORY.md").exists()


@pytest.mark.asyncio
async def test_append_preserves_container_candidate_marker_in_overlay(tmp_path: Path) -> None:
    agent_id = uuid.uuid4()

    result = await append_t3_memory_candidate(
        agent_id,
        category="strategy",
        content="Research -> design -> verify workflow reduces review iterations",
        source_refs=["t2:segment/summary.md#entry:xyz"],
        container_candidate="skill_candidate",
        proposed_by="heartbeat",
        data_root=tmp_path,
    )

    assert result.status == "overlay"
    body = (tmp_path / str(agent_id) / "memory" / "explicit" / "entries" / f"{result.entry_id}.md").read_text(
        encoding="utf-8"
    )
    assert "skill_candidate" in body


def test_write_file_tool_refuses_memory_paths(tmp_path: Path) -> None:
    from app.services.agent_tool_domains.workspace import _write_file

    ws = tmp_path / "ws"
    ws.mkdir()

    for rel in (
        "memory/t3/user.md",
        "memory/t3/capabilities.md",
        "memory/explicit/MEMORY.md",
        "memory/t2/sessions/session-1/segments/seg-1/summary.md",
    ):
        out = _write_file(ws, rel, "manual bypass")
        assert "governed" in out.lower() or "managed by platform services" in out
        assert not (ws / rel).exists()


def test_edit_and_delete_refuse_platform_managed_memory_paths(tmp_path: Path) -> None:
    from app.services.agent_tool_domains.workspace import _delete_file, _edit_file

    ws = tmp_path / "ws"
    target = ws / "memory" / "t3" / "user.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# T3 User\n", encoding="utf-8")

    edit_out = _edit_file(ws, "memory/t3/user.md", old_text="T3 User", new_text="tampered")
    delete_out = _delete_file(ws, "memory/t3/user.md")

    assert "managed by platform services" in edit_out or "governed" in edit_out.lower()
    assert "managed by platform services" in delete_out or "governed" in delete_out.lower()
    assert target.exists()
    assert "tampered" not in target.read_text(encoding="utf-8")


def test_workspace_files_outside_memory_still_writable(tmp_path: Path) -> None:
    from app.services.agent_tool_domains.workspace import _write_file

    ws = tmp_path / "ws"
    ws.mkdir()
    out = _write_file(ws, "workspace/report.md", "# Report")
    assert "written" in out.lower() or "success" in out.lower()
    assert (ws / "workspace" / "report.md").read_text(encoding="utf-8") == "# Report"
