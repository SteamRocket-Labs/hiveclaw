"""Part D tests: profile-plane write side (spec §3.2/§3.3/§4.4).

工序 1/2 ride-along: the T2 labels call now also carries four-plane signals
(self-signal, per-plane nutrients, milestone criteria hits) — two logical
steps, one LLM call. 工序 3 teaching lives in HEARTBEAT.md: operation patches
with an evidence-based motif/scenario/edge-case balance chosen by the model,
plus counter-example demotion on negative feedback. Feedback itself reaches the batch through the
explicit overlay (session_feedback → overlay → T3 batch) — pinned here as the
production wiring for demotion input.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.memory.t2.prompts import LEARNING_BRAIN_LABELS_PROMPT


def test_labels_prompt_teaches_four_plane_signals() -> None:
    assert "four_plane_signals" in LEARNING_BRAIN_LABELS_PROMPT
    assert "self_signal" in LEARNING_BRAIN_LABELS_PROMPT
    # nutrients routed per plane: self / profiles / knowledge / milestones
    for plane in ("self", "profiles", "knowledge", "milestones"):
        assert plane in LEARNING_BRAIN_LABELS_PROMPT
    assert "milestone_signal" in LEARNING_BRAIN_LABELS_PROMPT


def test_labels_prompt_teaches_milestone_criteria() -> None:
    """判据①②③ ride the same labels call (工序 2, spec §3.5)."""
    text = LEARNING_BRAIN_LABELS_PROMPT
    assert "owner_feedback" in text  # ① explicit owner positive/negative feedback
    assert "major_failure" in text  # ② big failure / rework
    assert "first_success" in text  # ③ first time a task class succeeds


@pytest.mark.asyncio
async def test_labels_with_four_plane_signals_pass_validation(tmp_path: Path) -> None:
    """New optional labels nodes must not break the T2 package validator."""
    from app.memory.t0.ledger import append_t0_session_event
    from app.memory.t2.segment_package import build_t2_segment_package

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    event = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="user_message",
        role="user",
        content="带四区信号的段。",
        source="web",
        data_root=tmp_path,
    )

    def _summary(sb: dict) -> str:
        ref = sb["source_refs"][0]
        return f"""<t2_summary schema_version="t2.summary.v1" package_id="{sb["package_id"]}" session_id="{sb["session_id"]}" t0_segment_id="{sb["t0_segment_id"]}" status="closed">
  <source_refs><source_ref uri="{ref["uri"]}" path="{ref["path"]}" sha256="{ref["sha256"]}"/></source_refs>
  <segment_state value="complete"><reason>ok</reason></segment_state>
  <scenario><title>t</title><context>c</context></scenario>
  <events><event id="evt-1" type="instruction" salience="high"><summary>s</summary><evidence_refs><source_ref uri="{ref["uri"]}"/></evidence_refs></event></events>
  <facts><fact evidence_strength="source_backed">f</fact></facts>
  <decisions/><corrections/><method_trace/><artifacts/><open_questions/><short_term_carryover/>
  <continuity><open_threads/></continuity>
  <promotion_hints><hint target="t3_candidate" reason="r"/></promotion_hints>
</t2_summary>"""

    def _labels(sb: dict) -> str:
        ref = sb["source_refs"][0]["uri"]
        return f"""<t2_labels schema_version="t2.labels.v1" package_id="{sb["package_id"]}" session_id="{sb["session_id"]}" t2_segment_id="{sb["t0_segment_id"]}">
  <control_metadata>
    <source_integrity>complete</source_integrity><sensitivity>PL1</sensitivity>
    <principal_scope>direct_owner</principal_scope><package_status>closed</package_status>
    <confidence>0.95</confidence><continuity_state>standalone</continuity_state>
    <systems><system>memory</system></systems><risk_flags/>
  </control_metadata>
  <four_plane_signals>
    <self_signal present="true">失败模式候选:需求含糊时自行猜测。</self_signal>
    <nutrients>
      <nutrient plane="self">失败模式:爱猜</nutrient>
      <nutrient plane="knowledge">L2 rollup 概念更新</nutrient>
    </nutrients>
    <milestone_signal criteria="owner_feedback">owner 明确好评本次交付。</milestone_signal>
  </four_plane_signals>
  <event_labels>
    <event_label event_ref="evt-1">
      <event_type>instruction</event_type><memory_domain>preference_memory</memory_domain>
      <outcome>accepted</outcome><actionability>t3_candidate</actionability>
      <stability>stable</stability><completeness>closed</completeness><salience>high</salience>
      <source_refs><source_ref uri="{ref}"/></source_refs>
    </event_label>
  </event_labels>
</t2_labels>"""

    def _review(sb: dict) -> str:
        ref = sb["source_refs"][0]["uri"]
        return f"""<t2_review schema_version="t2.review.v1" package_id="{sb["package_id"]}" reviewer="memory_gate_agent">
  <decision>approved</decision><allowed_next>t3_intake</allowed_next>
  <review_rubric schema_version="t2.review_rubric.v1">
    <score name="summary_fidelity" value="0.95"/><score name="source_ref_coverage" value="0.95"/>
    <score name="label_alignment" value="0.90"/><score name="safety_scope" value="1.00"/>
    <score name="package_closure" value="0.90"/><review_score>0.95</review_score>
  </review_rubric>
  <evidence_coverage>complete</evidence_coverage><hallucination_risk>low</hallucination_risk>
  <label_quality>pass</label_quality><continuity_result>standalone</continuity_result>
  <sensitivity_result>pass</sensitivity_result><issues/><required_changes/>
  <source_refs_checked><source_ref uri="{ref}"/></source_refs_checked>
</t2_review>"""

    async def summary_agent(sb):
        return _summary(sb)

    async def labels_agent(sb, _s):
        return _labels(sb)

    async def gate_agent(sb, _s, _l):
        return _review(sb)

    result = await build_t2_segment_package(
        data_root=tmp_path,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        t0_segment_id=event.segment_id,
        summary_agent=summary_agent,
        learning_brain=labels_agent,
        memory_gate=gate_agent,
    )

    assert result.status == "committed", result.issues
    labels_text = (result.package_dir / "labels.md").read_text(encoding="utf-8")
    assert "four_plane_signals" in labels_text


def test_heartbeat_template_teaches_profile_entries() -> None:
    template = Path("app/templates/HEARTBEAT.md").read_text(encoding="utf-8")
    assert "upsert_entry" in template
    assert "memory/self/self.md" in template
    assert "memory/profiles/owner.md" in template
    # The model chooses the semantic balance; fixed quotas must never substitute.
    assert "no fixed percentage" in template
    assert "80%" not in template and "15%" not in template and "5%" not in template
    # counter-example demotion on negative feedback (spec §3.2/§4.4)
    assert "反例下调" in template or "counter-example" in template


@pytest.mark.asyncio
async def test_feedback_overlay_reaches_t3_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Production demotion input: session feedback → overlay → T3 batch."""
    from app.memory.t3_consolidation import build_t3_consolidation_batch, discover_pending_t3_sources
    from app.memory.write_gate import MemoryWriteDecision
    from app.services.session_feedback import write_session_feedback_overlay

    async def accept_reviewed_memory(content: str, *, category: str, **_kwargs) -> MemoryWriteDecision:
        return MemoryWriteDecision(
            original_content=content,
            content=content,
            category=category,
            sensitivity="PL1_public",
            metadata={"threat_gate_method": "test_model_reviewer"},
        )

    monkeypatch.setattr(
        "app.memory.explicit_overlay.prepare_memory_write_with_llm",
        accept_reviewed_memory,
    )

    agent_id = uuid4()
    await write_session_feedback_overlay(
        agent_id,
        category="feedback",
        content="Owner marked an agent session misleading. 猜测了需求导致返工。",
        source_refs="fb-decision-123",
        evidence="misleading",
        data_root=tmp_path,
    )

    pending = discover_pending_t3_sources(agent_id=agent_id, data_root=tmp_path)
    assert pending.explicit_entry_ids

    result = build_t3_consolidation_batch(
        agent_id=agent_id,
        data_root=tmp_path,
        package_dirs=[],
        explicit_entry_ids=list(pending.explicit_entry_ids),
    )
    bundle = json.loads((result.job_dir / "source_bundle.json").read_text(encoding="utf-8"))
    entries = bundle["explicit_overlay_entries"]
    assert any("misleading" in str(entry.get("content", "")) for entry in entries)
