"""Tests for 切口④: Work Ledger verified findings → durable T2 memory via the gate.

docs/agent-task-cognitive-scaffold.md §7 切口④ + §8 invariant 4: when a task
finishes, the ledger's *verified* findings (and failure-learnings) settle into
long-term memory — but **only through the Memory Control Plane write gate**
(``prepare_memory_write``): PL4 credentials are rejected, sensitivity is
classified, lifecycle/evidence metadata is stamped. These tests exercise the
**real gate** (no mocks) against a temp data_root, proving the wiring reuses the
gate rather than bypassing it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


def _settings_patch(mp: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect AGENT_DATA_DIR for both the ledger reader and the T2 writer.

    ``consolidate_ledger_findings_to_t2`` reads the ledger via the work-ledger
    service (which uses ``app.services.agent_work_ledger.get_settings``) and then
    writes via ``_append_to_learnings`` (which uses
    ``app.services.extract_agent.get_settings``). Patch both bindings so the whole
    path stays inside tmp while exercising the real production code.
    """

    fake = lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path))  # noqa: E731
    mp.setattr("app.services.agent_work_ledger.get_settings", fake)
    mp.setattr("app.services.extract_agent.get_settings", fake)


# ── pure mapper ──────────────────────────────────────────────────────────────


def test_mapper_only_promotes_verified_findings():
    from app.services.extract_agent import ledger_findings_to_extractions

    ledger = {
        "findings": [
            {"summary": "Vendor API caps pages at 100 rows", "trust": "verified", "source_refs": ["ws/notes.md"]},
            {"summary": "Maybe the cache is stale", "trust": "unverified"},
            {"summary": "", "trust": "verified"},  # empty → skipped
        ],
        "failures": [],
    }
    extractions = ledger_findings_to_extractions(ledger)
    assert len(extractions) == 1
    assert extractions[0]["content"] == "Vendor API caps pages at 100 rows"
    assert extractions[0]["category"] == "reference"
    assert extractions[0]["source_refs"] == "ws/notes.md"


def test_mapper_promotes_failures_with_a_next_strategy_only():
    from app.services.extract_agent import ledger_findings_to_extractions

    ledger = {
        "findings": [],
        "failures": [
            {"error": "bulk export timed out", "next_strategy": "paginate at 100", "resolved": False},
            {"error": "bare error with no lesson", "next_strategy": "", "resolved": False},  # skipped
            {"error": "already handled", "next_strategy": "retry", "resolved": True},  # resolved → skipped
        ],
    }
    extractions = ledger_findings_to_extractions(ledger)
    assert len(extractions) == 1
    assert extractions[0]["category"] == "blocked_pattern"
    assert "paginate at 100" in extractions[0]["content"]


def test_mapper_empty_for_no_ledger():
    from app.services.extract_agent import ledger_findings_to_extractions

    assert ledger_findings_to_extractions(None) == []
    assert ledger_findings_to_extractions({"findings": [], "failures": []}) == []


# ── orchestrator through the real write gate ──────────────────────────────────


def test_verified_finding_settles_into_t2_through_gate(tmp_path, monkeypatch):
    from app.services.agent_work_ledger import append_agent_work_ledger_finding
    from app.services.extract_agent import consolidate_ledger_findings_to_t2
    from app.memory.t2_store import load_t2_entries

    agent_id = uuid4()
    _settings_patch(monkeypatch, tmp_path)

    append_agent_work_ledger_finding(
        agent_id=agent_id,
        finding_type="finding",
        summary="The export endpoint rate-limits at 60 req/min",
        trust="verified",
        data_root=tmp_path,
    )

    written = consolidate_ledger_findings_to_t2(agent_id, data_root=tmp_path)
    assert written == 1

    entries, _ = load_t2_entries(tmp_path, agent_id)
    contents = [e["content"] for e in entries]
    assert "The export endpoint rate-limits at 60 req/min" in contents
    # The gate stamped lifecycle metadata (entry_id) on the persisted entry.
    settled = next(e for e in entries if e["content"] == "The export endpoint rate-limits at 60 req/min")
    assert settled.get("entry_id")
    assert settled.get("sensitivity") == "PL1_public"


def test_unverified_finding_does_not_reach_t2(tmp_path, monkeypatch):
    from app.services.agent_work_ledger import append_agent_work_ledger_finding
    from app.services.extract_agent import consolidate_ledger_findings_to_t2
    from app.memory.t2_store import load_t2_entries

    agent_id = uuid4()
    _settings_patch(monkeypatch, tmp_path)

    append_agent_work_ledger_finding(
        agent_id=agent_id,
        finding_type="finding",
        summary="Hunch: the bug is in the parser",
        trust="unverified",
        data_root=tmp_path,
    )

    written = consolidate_ledger_findings_to_t2(agent_id, data_root=tmp_path)
    assert written == 0
    entries, _ = load_t2_entries(tmp_path, agent_id)
    assert entries == []


def test_pl4_credential_in_finding_is_rejected_by_gate(tmp_path, monkeypatch):
    """§8 invariant 4: a verified finding carrying a PL4 credential is rejected — never persisted."""
    from app.services.agent_work_ledger import append_agent_work_ledger_finding
    from app.services.extract_agent import consolidate_ledger_findings_to_t2
    from app.memory.t2_store import load_t2_entries

    agent_id = uuid4()
    _settings_patch(monkeypatch, tmp_path)

    # An OpenAI-style secret token, built at runtime so no literal key lives in
    # source. PrivacyLayer classifies this PL4 and the write gate rejects it with
    # zero retention.
    fake_secret = "sk-" + ("a" * 30)
    append_agent_work_ledger_finding(
        agent_id=agent_id,
        finding_type="finding",
        summary=f"The prod API key is {fake_secret}",
        trust="verified",
        data_root=tmp_path,
    )

    written = consolidate_ledger_findings_to_t2(agent_id, data_root=tmp_path)
    assert written == 0  # gate rejected the PL4 entry

    entries, _ = load_t2_entries(tmp_path, agent_id)
    # Nothing persisted, and crucially the raw secret never landed in any T2 file.
    assert entries == []
    learnings_dir = tmp_path / str(agent_id) / "memory" / "learnings"
    if learnings_dir.exists():
        for md in learnings_dir.glob("*.md"):
            assert fake_secret not in md.read_text(encoding="utf-8")


def test_pl3_sensitive_finding_is_classified_when_settled(tmp_path, monkeypatch):
    """A verified-but-sensitive finding still settles, tagged PL3 by the gate (not dropped)."""
    from app.services.agent_work_ledger import append_agent_work_ledger_finding
    from app.services.extract_agent import consolidate_ledger_findings_to_t2
    from app.memory.t2_store import load_t2_entries

    agent_id = uuid4()
    _settings_patch(monkeypatch, tmp_path)

    append_agent_work_ledger_finding(
        agent_id=agent_id,
        finding_type="finding",
        summary="The Q3 salary review is scheduled for next month",
        trust="verified",
        data_root=tmp_path,
    )

    written = consolidate_ledger_findings_to_t2(agent_id, data_root=tmp_path)
    assert written == 1
    entries, _ = load_t2_entries(tmp_path, agent_id)
    settled = next(e for e in entries if "salary review" in e["content"])
    assert settled.get("sensitivity") == "PL3_sensitive"


def test_consolidation_is_idempotent_on_dedup(tmp_path, monkeypatch):
    """Re-running consolidation does not double-write the same finding (T2 dedups)."""
    from app.services.agent_work_ledger import append_agent_work_ledger_finding
    from app.services.extract_agent import consolidate_ledger_findings_to_t2
    from app.memory.t2_store import load_t2_entries

    agent_id = uuid4()
    _settings_patch(monkeypatch, tmp_path)

    append_agent_work_ledger_finding(
        agent_id=agent_id,
        finding_type="finding",
        summary="Pagination cursor is stable across requests",
        trust="verified",
        data_root=tmp_path,
    )

    assert consolidate_ledger_findings_to_t2(agent_id, data_root=tmp_path) == 1
    # Second pass: the entry already exists → T2 dedup means zero new writes.
    assert consolidate_ledger_findings_to_t2(agent_id, data_root=tmp_path) == 0

    entries, _ = load_t2_entries(tmp_path, agent_id)
    assert sum(1 for e in entries if "Pagination cursor is stable" in e["content"]) == 1


# ── SESSION_CLOSE wiring (the actual trigger point) ───────────────────────────


@pytest.mark.asyncio
async def test_session_close_hook_settles_ledger_findings_to_t2(tmp_path, monkeypatch):
    """The SESSION_CLOSE handler runs ledger→T2 consolidation through the real gate."""
    from app.memory.t2_store import load_t2_entries
    from app.runtime.hooks import HookContext, HookEvent
    from app.runtime.hooks_setup import _t0_session_close
    from app.services.agent_work_ledger import append_agent_work_ledger_finding

    agent_id = uuid4()
    _settings_patch(monkeypatch, tmp_path)
    # The handler also writes T0 logs / session memory; point those at tmp too and
    # keep the test focused on the ledger→T2 path.
    monkeypatch.setattr("app.runtime.hooks_setup.write_t0_log", lambda *a, **k: None)
    monkeypatch.setattr("app.runtime.hooks_setup.update_session_memory", lambda *a, **k: None)

    append_agent_work_ledger_finding(
        agent_id=agent_id,
        finding_type="finding",
        summary="Webhook retries use exponential backoff capped at 5 minutes",
        trust="verified",
        data_root=tmp_path,
    )

    await _t0_session_close(
        HookContext(
            event=HookEvent.SESSION_CLOSE,
            agent_id=str(agent_id),
            session_id="sess-close-1",
            source="web",
            messages=[{"role": "user", "content": "thanks"}],
            metadata={"reason": "client_close"},
        )
    )

    entries, _ = load_t2_entries(tmp_path, agent_id)
    assert any("exponential backoff capped at 5 minutes" in e["content"] for e in entries)
