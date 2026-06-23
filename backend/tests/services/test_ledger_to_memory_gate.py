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

import json
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
    mp.setattr("app.memory.t0.ledger.get_settings", fake)
    mp.setattr("app.config.get_settings", fake)
    mp.setattr("app.runtime.hooks_setup.get_settings", fake, raising=False)


def _approved_review_xml(*, package_id: str, ref: str) -> str:
    return f"""<t2_review schema_version="t2.review.v1" package_id="{package_id}" reviewer="memory_gate_agent">
  <decision>approved</decision>
  <allowed_next>t3_intake</allowed_next>
  <review_rubric schema_version="t2.review_rubric.v1">
    <score name="summary_fidelity" value="0.95"/>
    <score name="source_ref_coverage" value="0.95"/>
    <score name="label_alignment" value="0.90"/>
    <score name="safety_scope" value="1.00"/>
    <score name="package_closure" value="0.90"/>
    <review_score>0.95</review_score>
  </review_rubric>
  <source_refs_checked><source_ref uri="{ref}"/></source_refs_checked>
</t2_review>"""


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
    assert extractions[0]["evidence"] == "agent_ledger_verified"


def test_mapper_requires_source_refs_for_verified_findings():
    from app.services.extract_agent import ledger_findings_to_extractions

    ledger = {
        "findings": [
            {"summary": "Agent asserted this without evidence", "trust": "verified"},
            {"summary": "Evidence-backed finding", "trust": "verified", "source_refs": ["workspace/evidence.md"]},
        ],
        "failures": [],
    }

    extractions = ledger_findings_to_extractions(ledger)
    assert [item["content"] for item in extractions] == ["Evidence-backed finding"]


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
    # Agent-authored ledger content must never carry the runtime-only
    # tool_verified label (same principle as findings' agent_ledger_verified).
    assert extractions[0]["evidence"] == "agent_ledger_observed"


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
        source_refs=["workspace/rate-limit-notes.md"],
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
    assert settled.get("evidence") == "agent_ledger_verified"
    assert settled.get("source_refs") == ["workspace/rate-limit-notes.md"]


def test_verified_finding_without_source_refs_does_not_reach_t2(tmp_path, monkeypatch):
    from app.services.agent_work_ledger import append_agent_work_ledger_finding
    from app.services.extract_agent import consolidate_ledger_findings_to_t2
    from app.memory.t2_store import load_t2_entries

    agent_id = uuid4()
    _settings_patch(monkeypatch, tmp_path)

    append_agent_work_ledger_finding(
        agent_id=agent_id,
        finding_type="finding",
        summary="The model can self-assert this as verified",
        trust="verified",
        data_root=tmp_path,
    )

    written = consolidate_ledger_findings_to_t2(agent_id, data_root=tmp_path)
    assert written == 0
    entries, _ = load_t2_entries(tmp_path, agent_id)
    assert entries == []


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
        source_refs=["workspace/unsafe.txt"],
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
        source_refs=["workspace/hr-notes.md"],
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
        source_refs=["workspace/pagination.md"],
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
    """SESSION_CLOSE puts verified ledger findings into the canonical T2 source bundle."""
    from app.memory.t2_store import load_t2_entries
    from app.memory.t0.ledger import append_t0_session_event, replay_t0_session_events
    from app.runtime.hooks import HookContext, HookEvent
    from app.runtime.hooks_setup import _t0_session_close
    from app.services.agent_work_ledger import append_agent_work_ledger_finding

    agent_id = uuid4()
    tenant_id = uuid4()
    _settings_patch(monkeypatch, tmp_path)
    # The handler also updates session memory; keep the test focused on the
    # ledger→T2 path. T0 itself should seal the session ledger, not call the
    # retired legacy log writer.
    monkeypatch.setattr("app.runtime.hooks_setup.update_session_memory", lambda *a, **k: None)
    append_t0_session_event(
        agent_id=agent_id,
        session_id="sess-close-1",
        event_type="user_message",
        role="user",
        content="thanks",
        tenant_id=tenant_id,
        data_root=tmp_path,
    )

    append_agent_work_ledger_finding(
        agent_id=agent_id,
        finding_type="finding",
        summary="Webhook retries use exponential backoff capped at 5 minutes",
        trust="verified",
        source_refs=["workspace/webhook-notes.md"],
        session_id="sess-close-1",
        data_root=tmp_path,
    )

    async def fake_model_config(_tenant_id):
        return {"provider": "fake", "model": "fake"}

    async def fake_run_agent(**kwargs):
        payload = kwargs["payload"]
        source_bundle = payload if kwargs["phase"] == "summary" else payload["source_bundle"]
        ref = source_bundle["source_refs"][0]["uri"]
        package_id = source_bundle["package_id"]
        if kwargs["phase"] == "summary":
            assert source_bundle["work_ledger"]["findings"][0]["summary"] == (
                "Webhook retries use exponential backoff capped at 5 minutes"
            )
            return f"""<t2_summary schema_version="t2.summary.v1" package_id="{package_id}" status="closed">
  <segment_state value="complete">complete</segment_state>
  <continuity><state>standalone</state><reason>该 session close 片段自包含且可独立总结。</reason></continuity>
  <summary>Webhook retries use exponential backoff capped at 5 minutes.</summary>
  <source_refs><source_ref uri="{ref}"/></source_refs>
</t2_summary>"""
        if kwargs["phase"] == "labels":
            return f"""<t2_labels schema_version="t2.labels.v1" package_id="{package_id}">
  <package_status>closed</package_status>
  <continuity_state>standalone</continuity_state>
  <source_refs><source_ref uri="{ref}"/></source_refs>
</t2_labels>"""
        return _approved_review_xml(package_id=package_id, ref=ref)

    monkeypatch.setattr("app.services.memory_service._get_summary_model_config", fake_model_config)
    monkeypatch.setattr("app.memory.t2.segment_package._run_t2_llm_agent", fake_run_agent)

    await _t0_session_close(
        HookContext(
            event=HookEvent.SESSION_CLOSE,
            agent_id=str(agent_id),
            session_id="sess-close-1",
            source="web",
            messages=[{"role": "user", "content": "thanks"}],
            metadata={"reason": "client_close", "tenant_id": str(tenant_id)},
        )
    )

    entries, _ = load_t2_entries(tmp_path, agent_id)
    assert entries == []
    t0_events = replay_t0_session_events(agent_id=agent_id, session_id="sess-close-1", data_root=tmp_path)
    assert [event.event_type for event in t0_events] == ["user_message", "segment_boundary"]
    assert not list((tmp_path / str(agent_id) / "logs").glob("**/chat-*.md"))
    package_root = tmp_path / str(agent_id) / "memory" / "t2" / "sessions" / "sess-close-1" / "segments"
    package_dir = next(package_root.iterdir())
    assert (package_dir / "summary.md").exists()
    staging_bundle = next((tmp_path / str(agent_id) / "memory" / ".staging" / "t2_jobs").glob("*/source_bundle.json"))
    source_bundle = json.loads(staging_bundle.read_text(encoding="utf-8"))
    assert source_bundle["work_ledger"]["source_refs"] == ["workspace/webhook-notes.md"]
