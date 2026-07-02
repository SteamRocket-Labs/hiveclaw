from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4
import xml.etree.ElementTree as ET

import pytest

from app.memory.t0.ledger import append_t0_session_event
from app.services.agent_work_ledger import append_agent_work_ledger_finding


def _assert_xml_block(path: Path, tag: str) -> ET.Element:
    text = path.read_text(encoding="utf-8")
    start = text.index(f"<{tag}")
    end = text.index(f"</{tag}>") + len(f"</{tag}>")
    return ET.fromstring(text[start:end])


def _approved_review_xml(*, package_id: str | None, ref: str, allowed_next: str = "t3_intake") -> str:
    package_attr = f' package_id="{package_id}"' if package_id else ""
    return f"""<t2_review schema_version="t2.review.v1"{package_attr} reviewer="memory_gate_agent">
  <decision>approved</decision>
  <allowed_next>{allowed_next}</allowed_next>
  <review_rubric schema_version="t2.review_rubric.v1">
    <score name="summary_fidelity" value="0.95"/>
    <score name="source_ref_coverage" value="0.95"/>
    <score name="label_alignment" value="0.90"/>
    <score name="safety_scope" value="1.00"/>
    <score name="package_closure" value="0.90"/>
    <review_score>0.95</review_score>
  </review_rubric>
  <evidence_coverage>complete</evidence_coverage>
  <hallucination_risk>low</hallucination_risk>
  <label_quality>pass</label_quality>
  <continuity_result>{"requires_episode_stitching" if allowed_next == "episode_stitching" else "standalone"}</continuity_result>
  <sensitivity_result>pass</sensitivity_result>
  <issues/>
  <required_changes/>
  <source_refs_checked><source_ref uri="{ref}"/></source_refs_checked>
</t2_review>"""


def _summary_xml(source_bundle: dict, *, status: str = "closed", segment_state: str = "complete") -> str:
    ref = source_bundle["source_refs"][0]
    return f"""# T2 Segment Summary

<t2_summary schema_version="t2.summary.v1" package_id="{source_bundle["package_id"]}" session_id="{source_bundle["session_id"]}" t0_segment_id="{source_bundle["t0_segment_id"]}" status="{status}">
  <source_refs>
    <source_ref uri="{ref["uri"]}" path="{ref["path"]}" sha256="{ref["sha256"]}"/>
  </source_refs>
  <segment_state value="{segment_state}">
    <reason>测试片段状态。</reason>
  </segment_state>
  <scenario><title>测试场景</title><context>用于 T2 package builder 测试。</context></scenario>
  <events><event id="evt-1" type="instruction" salience="high"><summary>测试事件。</summary><evidence_refs><source_ref uri="{ref["uri"]}"/></evidence_refs></event></events>
  <facts><fact evidence_strength="source_backed">测试事实。</fact></facts>
  <decisions/>
  <corrections/>
  <method_trace/>
  <artifacts/>
  <open_questions/>
  <short_term_carryover/>
  <continuity>
    <open_threads/>
  </continuity>
  <promotion_hints><hint target="t3_candidate" reason="closed standalone package"/></promotion_hints>
</t2_summary>
"""


def _labels_xml(source_bundle: dict, *, package_status: str = "closed", continuity_state: str = "standalone") -> str:
    ref = source_bundle["source_refs"][0]["uri"]
    return f"""# T2 Segment Labels

<t2_labels schema_version="t2.labels.v1" package_id="{source_bundle["package_id"]}" session_id="{source_bundle["session_id"]}" t2_segment_id="{source_bundle["t0_segment_id"]}">
  <control_metadata>
    <source_integrity>complete</source_integrity>
    <sensitivity>PL1</sensitivity>
    <principal_scope>direct_owner</principal_scope>
    <package_status>{package_status}</package_status>
    <confidence>0.95</confidence>
    <continuity_state>{continuity_state}</continuity_state>
    <systems><system>memory</system></systems>
    <risk_flags/>
  </control_metadata>
  <event_labels>
    <event_label event_ref="evt-1">
      <event_type>instruction</event_type>
      <memory_domain>preference_memory</memory_domain>
      <outcome>accepted</outcome>
      <actionability>t3_candidate</actionability>
      <stability>stable</stability>
      <completeness>{package_status}</completeness>
      <salience>high</salience>
      <source_refs><source_ref uri="{ref}"/></source_refs>
    </event_label>
  </event_labels>
</t2_labels>
"""


def test_source_bundle_excludes_projection_only_t0_events(tmp_path: Path) -> None:
    from app.memory.t2.segment_package import _build_source_bundle

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    projection = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="assistant_message",
        role="assistant",
        content="copied answer from source branch",
        source="conversation_branch",
        metadata={"projection_only": True, "semantic_memory_eligible": False},
        data_root=tmp_path,
    )
    real_event = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="assistant_message",
        role="assistant",
        content="new regenerated answer",
        source="web_chat",
        metadata={"branch_mode": "regenerate"},
        data_root=tmp_path,
    )

    source_bundle = _build_source_bundle(
        root=tmp_path,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        t0_segment_id=projection.segment_id,
        package_id="pkg-projection-filter",
        session_lineage={
            "root_session_id": str(session_id),
            "parent_session_id": "source-session-1",
            "branch_mode": "rewind",
            "source_session_id": "source-session-1",
            "anchor_event_id": "anchor-event-1",
            "anchor_sequence": 7,
        },
    )

    assert source_bundle["source_range"] == {"start_sequence": real_event.sequence, "end_sequence": real_event.sequence}
    assert source_bundle["source_refs"][0]["uri"].endswith(f"#seq={real_event.sequence}..{real_event.sequence}")
    assert [event["content"] for event in source_bundle["t0_events"]] == ["new regenerated answer"]
    assert source_bundle["lineage"]["branch_mode"] == "rewind"
    assert source_bundle["lineage"]["anchor_sequence"] == 7
    assert source_bundle["visible_source_view"]["semantic_sequences"] == [real_event.sequence]
    assert source_bundle["excluded_refs"][0]["reason"] == "semantic_memory_eligible=false"
    assert source_bundle["visible_source_view"]["lineage_risk_flags"] == [
        "branch_mode=rewind",
        "source_session_id=source-session-1",
    ]


@pytest.mark.asyncio
async def test_branch_lineage_requires_independent_t2_review(tmp_path: Path) -> None:
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
        content="在 rollback branch 上重新确认新的方案。",
        source="web",
        metadata={"branch_mode": "rewind", "source_session_id": "source-session-1"},
        data_root=tmp_path,
    )

    async def summary_agent(source_bundle: dict) -> str:
        return _summary_xml(source_bundle)

    async def labels(source_bundle: dict, _summary_md: str) -> str:
        return _labels_xml(source_bundle)

    result = await build_t2_segment_package(
        data_root=tmp_path,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        t0_segment_id=event.segment_id,
        summary_agent=summary_agent,
        learning_brain=labels,
        memory_gate=None,
    )

    assert result.status == "held"
    assert result.issues == ("independent Memory Gate required: all T2 reviews must be LLM-authored",)
    assert not result.package_dir.exists()


def test_source_bundle_rejects_segments_without_semantic_t0_events(tmp_path: Path) -> None:
    from app.memory.t2.segment_package import _build_source_bundle

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    projection = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="assistant_message",
        role="assistant",
        content="copied answer from source branch",
        source="conversation_branch",
        metadata={"projection_only": True, "semantic_memory_eligible": False},
        data_root=tmp_path,
    )

    with pytest.raises(ValueError, match="no semantic T0 events"):
        _build_source_bundle(
            root=tmp_path,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            t0_segment_id=projection.segment_id,
            package_id="pkg-empty-projection",
        )


@pytest.mark.asyncio
async def test_build_t2_segment_package_commits_agent_outputs_atomically(tmp_path: Path) -> None:
    from app.memory.t2.segment_package import build_t2_segment_package

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    first = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="user_message",
        role="user",
        content="以后架构改造先讨论，确认后再改文档。",
        source="web",
        data_root=tmp_path,
    )
    append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="assistant_message",
        role="assistant",
        content="已确认：讨论优先，落盘走受控路径。",
        source="web",
        data_root=tmp_path,
    )

    async def summary_agent(source_bundle: dict) -> str:
        return _summary_xml(source_bundle)

    async def learning_brain(source_bundle: dict, summary_md: str) -> str:
        return _labels_xml(source_bundle)

    async def memory_gate(source_bundle: dict, summary_md: str, labels_md: str) -> str:
        ref = source_bundle["source_refs"][0]["uri"]
        return "# T2 Segment Review\n\n" + _approved_review_xml(package_id=source_bundle["package_id"], ref=ref)

    result = await build_t2_segment_package(
        data_root=tmp_path,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        t0_segment_id=first.segment_id,
        summary_agent=summary_agent,
        learning_brain=learning_brain,
        memory_gate=memory_gate,
    )

    package_dir = tmp_path / str(agent_id) / "memory" / "t2" / "sessions" / str(session_id) / "segments" / first.segment_id
    assert result.status == "committed"
    assert result.package_dir == package_dir
    assert sorted(path.name for path in package_dir.iterdir()) == [
        "labels.md",
        "manifest.json",
        "review.md",
        "summary.md",
    ]
    assert not (package_dir / "raw_refs.md").exists()
    assert not (tmp_path / str(agent_id) / "memory" / "learnings").exists()
    assert not (
        tmp_path / str(agent_id) / "memory" / "sessions" / str(session_id) / "segments" / first.segment_id
    ).exists()

    manifest = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "t2.segment-package.manifest.v1"
    assert manifest["package_status"] == "reviewed"
    assert manifest["source_refs"][0].startswith(f"t0://session/{session_id}/segment/{first.segment_id}#seq=1..")
    assert manifest["files"]["summary.md"]["sha256"]
    assert manifest["prompts"]["summary_prompt_version"] == "t2.summary_agent.v1"
    assert manifest["prompts"]["labels_prompt_version"] == "t2.learning_brain_labels.v2"
    assert manifest["review_mode"] == "independent_gate"
    assert manifest["prompts"]["review_prompt_version"] == "t2.memory_gate_review.v1"

    source_bundle_path = (
        tmp_path / str(agent_id) / "memory" / ".staging" / "t2_jobs" / result.job_id / "source_bundle.json"
    )
    source_bundle = json.loads(source_bundle_path.read_text(encoding="utf-8"))
    assert source_bundle["schema_version"] == "t2.source_bundle.v1"
    assert source_bundle["source_refs"][0]["path"].endswith(
        f"memory/t0/sessions/{session_id}/segments/{first.segment_id}/source.md"
    )
    assert source_bundle["source_refs"][0]["truth_path"].endswith(
        f"memory/t0/sessions/{session_id}/segments/{first.segment_id}/events.jsonl"
    )
    assert source_bundle["source_refs"][0]["truth_sha256"]
    assert source_bundle["t0_events"][0]["content"] == "以后架构改造先讨论，确认后再改文档。"
    assert source_bundle["t0_events"][0]["truth_path"].endswith(
        f"memory/t0/sessions/{session_id}/segments/{first.segment_id}/events.jsonl"
    )
    assert source_bundle["t0_events"][0]["event_hash"]

    summary_node = _assert_xml_block(package_dir / "summary.md", "t2_summary")
    labels_node = _assert_xml_block(package_dir / "labels.md", "t2_labels")
    assert summary_node.attrib["status"] == "closed"
    assert summary_node.find("segment_state").attrib["value"] == "complete"
    assert labels_node.findtext(".//confidence") == "0.95"
    assert labels_node.findtext(".//continuity_state") == "standalone"
    review_node = _assert_xml_block(package_dir / "review.md", "t2_review")
    assert review_node.attrib["reviewer"] == "memory_gate_agent"
    assert review_node.findtext("decision") == "approved"


@pytest.mark.asyncio
async def test_low_risk_t2_package_requires_memory_gate_review(tmp_path: Path) -> None:
    from app.memory.t2.segment_package import build_t2_segment_package

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    first = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="user_message",
        role="user",
        content="以后回复保持简洁，并在改代码前先写测试。",
        source="web",
        data_root=tmp_path,
    )

    async def summary_agent(source_bundle: dict) -> str:
        return _summary_xml(source_bundle)

    async def learning_brain(source_bundle: dict, summary_md: str) -> str:
        return _labels_xml(source_bundle)

    async def memory_gate(source_bundle: dict, summary_md: str, labels_md: str) -> str:
        ref = source_bundle["source_refs"][0]["uri"]
        return _approved_review_xml(package_id=source_bundle["package_id"], ref=ref)

    result = await build_t2_segment_package(
        data_root=tmp_path,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        t0_segment_id=first.segment_id,
        summary_agent=summary_agent,
        learning_brain=learning_brain,
        memory_gate=memory_gate,
    )

    package_dir = tmp_path / str(agent_id) / "memory" / "t2" / "sessions" / str(session_id) / "segments" / first.segment_id
    manifest = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
    review = _assert_xml_block(package_dir / "review.md", "t2_review")

    assert result.status == "committed"
    assert review.attrib["reviewer"] == "memory_gate_agent"
    assert review.findtext("decision") == "approved"
    assert manifest["review_mode"] == "independent_gate"
    assert manifest["prompts"]["review_prompt_version"] == "t2.memory_gate_review.v1"


@pytest.mark.asyncio
async def test_t2_package_holds_without_memory_gate_even_for_low_risk(tmp_path: Path) -> None:
    from app.memory.t2.segment_package import build_t2_segment_package

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    first = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="user_message",
        role="user",
        content="以后回复保持简洁，并在改代码前先写测试。",
        source="web",
        data_root=tmp_path,
    )

    async def summary_agent(source_bundle: dict) -> str:
        return _summary_xml(source_bundle)

    async def learning_brain(source_bundle: dict, summary_md: str) -> str:
        return _labels_xml(source_bundle)

    result = await build_t2_segment_package(
        data_root=tmp_path,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        t0_segment_id=first.segment_id,
        summary_agent=summary_agent,
        learning_brain=learning_brain,
        memory_gate=None,
    )

    report = json.loads((result.staging_dir / "platform_gate_report.json").read_text(encoding="utf-8"))
    assert result.status == "held"
    assert not result.package_dir.exists()
    assert report["issues"] == ["independent Memory Gate required: all T2 reviews must be LLM-authored"]


@pytest.mark.asyncio
async def test_high_risk_t2_package_requires_independent_memory_gate(tmp_path: Path) -> None:
    from app.memory.t2.segment_package import build_t2_segment_package

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    first = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="user_message",
        role="user",
        content="请记住这个跨公司权限边界的行为规则。",
        source="web",
        data_root=tmp_path,
    )

    async def summary_agent(source_bundle: dict) -> str:
        return _summary_xml(source_bundle)

    async def high_risk_labels(source_bundle: dict, summary_md: str) -> str:
        ref = source_bundle["source_refs"][0]["uri"]
        return f"""# T2 Segment Labels

<t2_labels schema_version="t2.labels.v1" package_id="{source_bundle["package_id"]}" session_id="{source_bundle["session_id"]}" t2_segment_id="{source_bundle["t0_segment_id"]}">
  <control_metadata>
    <source_integrity>complete</source_integrity>
    <sensitivity>PL3</sensitivity>
    <principal_scope>company</principal_scope>
    <package_status>closed</package_status>
    <confidence>0.92</confidence>
    <continuity_state>standalone</continuity_state>
    <systems><system>memory</system></systems>
    <risk_flags><risk_flag>cross_principal</risk_flag></risk_flags>
  </control_metadata>
  <event_labels>
    <event_label event_ref="evt-1">
      <event_type>instruction</event_type>
      <memory_domain>permission_memory</memory_domain>
      <outcome>accepted</outcome>
      <actionability>t3_candidate</actionability>
      <stability>stable</stability>
      <completeness>closed</completeness>
      <salience>high</salience>
      <source_refs><source_ref uri="{ref}"/></source_refs>
    </event_label>
  </event_labels>
</t2_labels>
"""

    result = await build_t2_segment_package(
        data_root=tmp_path,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        t0_segment_id=first.segment_id,
        summary_agent=summary_agent,
        learning_brain=high_risk_labels,
        memory_gate=None,
    )

    report = json.loads((result.staging_dir / "platform_gate_report.json").read_text(encoding="utf-8"))
    assert result.status == "held"
    assert any("independent Memory Gate required" in issue for issue in report["issues"])


@pytest.mark.asyncio
async def test_source_bundle_includes_verified_work_ledger_findings(tmp_path: Path) -> None:
    from app.memory.t2.segment_package import build_t2_segment_package

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = "session-with-ledger"
    first = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="user_message",
        role="user",
        content="检查 work ledger 是否进入 source bundle。",
        source="web",
        data_root=tmp_path,
    )
    append_agent_work_ledger_finding(
        agent_id=agent_id,
        session_id=session_id,
        finding_type="finding",
        summary="Webhook retries use exponential backoff capped at 5 minutes",
        trust="verified",
        source_refs=["workspace/webhook-notes.md"],
        data_root=tmp_path,
    )
    seen_source_bundle: dict | None = None

    async def summary_agent(source_bundle: dict) -> str:
        nonlocal seen_source_bundle
        seen_source_bundle = source_bundle
        return _summary_xml(source_bundle)

    async def labels(source_bundle: dict, _summary_md: str) -> str:
        ref = source_bundle["source_refs"][0]["uri"]
        return f"""# T2 Segment Labels

<t2_labels schema_version="t2.labels.v1" package_id="{source_bundle["package_id"]}" session_id="{source_bundle["session_id"]}" t2_segment_id="{source_bundle["t0_segment_id"]}">
  <control_metadata>
    <source_integrity>complete</source_integrity>
    <sensitivity>PL3</sensitivity>
    <principal_scope>company</principal_scope>
    <package_status>closed</package_status>
    <confidence>0.95</confidence>
    <continuity_state>standalone</continuity_state>
    <systems><system>memory</system></systems>
    <risk_flags><risk_flag>cross_principal</risk_flag></risk_flags>
  </control_metadata>
  <event_labels>
    <event_label event_ref="evt-1">
      <event_type>instruction</event_type>
      <memory_domain>permission_memory</memory_domain>
      <outcome>accepted</outcome>
      <actionability>t3_candidate</actionability>
      <stability>stable</stability>
      <completeness>closed</completeness>
      <salience>high</salience>
      <source_refs><source_ref uri="{ref}"/></source_refs>
    </event_label>
  </event_labels>
</t2_labels>
"""

    async def review(source_bundle: dict, _summary_md: str, _labels_md: str) -> str:
        ref = source_bundle["source_refs"][0]["uri"]
        return _approved_review_xml(package_id=None, ref=ref)

    await build_t2_segment_package(
        data_root=tmp_path,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        t0_segment_id=first.segment_id,
        summary_agent=summary_agent,
        learning_brain=labels,
        memory_gate=review,
    )

    assert seen_source_bundle is not None
    assert seen_source_bundle["work_ledger"]["findings"] == [
        {
            "id": "finding-1",
            "summary": "Webhook retries use exponential backoff capped at 5 minutes",
            "trust": "verified",
            "source_refs": ["workspace/webhook-notes.md"],
        }
    ]
    assert seen_source_bundle["work_ledger"]["source_refs"] == ["workspace/webhook-notes.md"]


@pytest.mark.asyncio
async def test_platform_gate_holds_invalid_agent_output_without_partial_commit(tmp_path: Path) -> None:
    from app.memory.t2.segment_package import build_t2_segment_package

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    first = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="user_message",
        role="user",
        content="不要把没有证据的内容写进记忆。",
        source="web",
        data_root=tmp_path,
    )

    async def invalid_summary(source_bundle: dict) -> str:
        return "<t2_summary schema_version='t2.summary.v1'></t2_summary>"

    async def labels(_source_bundle: dict, _summary_md: str) -> str:
        return "<t2_labels schema_version='t2.labels.v1'></t2_labels>"

    async def review(_source_bundle: dict, _summary_md: str, _labels_md: str) -> str:
        return "<t2_review schema_version='t2.review.v1'><decision>approved</decision></t2_review>"

    result = await build_t2_segment_package(
        data_root=tmp_path,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        t0_segment_id=first.segment_id,
        summary_agent=invalid_summary,
        learning_brain=labels,
        memory_gate=review,
    )

    package_dir = tmp_path / str(agent_id) / "memory" / "t2" / "sessions" / str(session_id) / "segments" / first.segment_id
    held_dir = tmp_path / str(agent_id) / "memory" / ".staging" / "t2_jobs" / result.job_id
    assert result.status == "held"
    assert not package_dir.exists()
    assert (held_dir / "summary.candidate.md").exists()
    assert (held_dir / "platform_gate_report.json").exists()
    report = json.loads((held_dir / "platform_gate_report.json").read_text(encoding="utf-8"))
    assert any("source_refs" in issue for issue in report["issues"])


@pytest.mark.asyncio
async def test_platform_gate_holds_approved_review_without_rubric(tmp_path: Path) -> None:
    from app.memory.t2.segment_package import build_t2_segment_package

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    first = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="user_message",
        role="user",
        content="显性数值判断必须有 rubric。",
        source="web",
        data_root=tmp_path,
    )

    async def summary_agent(source_bundle: dict) -> str:
        return _summary_xml(source_bundle)

    async def labels(source_bundle: dict, _summary_md: str) -> str:
        ref = source_bundle["source_refs"][0]["uri"]
        return f"""# T2 Segment Labels

<t2_labels schema_version="t2.labels.v1" package_id="{source_bundle["package_id"]}" session_id="{source_bundle["session_id"]}" t2_segment_id="{source_bundle["t0_segment_id"]}">
  <control_metadata>
    <source_integrity>complete</source_integrity>
    <sensitivity>PL3</sensitivity>
    <principal_scope>company</principal_scope>
    <package_status>closed</package_status>
    <confidence>0.95</confidence>
    <continuity_state>standalone</continuity_state>
    <systems><system>memory</system></systems>
    <risk_flags><risk_flag>cross_principal</risk_flag></risk_flags>
  </control_metadata>
  <event_labels>
    <event_label event_ref="evt-1">
      <event_type>instruction</event_type>
      <memory_domain>permission_memory</memory_domain>
      <outcome>accepted</outcome>
      <actionability>t3_candidate</actionability>
      <stability>stable</stability>
      <completeness>closed</completeness>
      <salience>high</salience>
      <source_refs><source_ref uri="{ref}"/></source_refs>
    </event_label>
  </event_labels>
</t2_labels>
"""

    async def review(source_bundle: dict, _summary_md: str, _labels_md: str) -> str:
        ref = source_bundle["source_refs"][0]["uri"]
        return f"""<t2_review schema_version='t2.review.v1'>
  <decision>approved</decision>
  <allowed_next>t3_intake</allowed_next>
  <score>0.95</score>
  <source_refs_checked><source_ref uri='{ref}'/></source_refs_checked>
</t2_review>"""

    result = await build_t2_segment_package(
        data_root=tmp_path,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        t0_segment_id=first.segment_id,
        summary_agent=summary_agent,
        learning_brain=labels,
        memory_gate=review,
    )

    report = json.loads((result.staging_dir / "platform_gate_report.json").read_text(encoding="utf-8"))
    assert result.status == "held"
    assert not result.package_dir.exists()
    assert any("review_rubric" in issue for issue in report["issues"])


@pytest.mark.asyncio
async def test_platform_gate_blocks_t3_intake_for_non_standalone_continuity(tmp_path: Path) -> None:
    from app.memory.t2.segment_package import build_t2_segment_package

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    first = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="user_message",
        role="user",
        content="我先去忙，回来继续。",
        source="web",
        data_root=tmp_path,
    )

    async def summary_agent(source_bundle: dict) -> str:
        return _summary_xml(source_bundle, status="rolling_checkpoint", segment_state="continuation")

    async def labels(source_bundle: dict, _summary_md: str) -> str:
        return _labels_xml(
            source_bundle,
            package_status="rolling_checkpoint",
            continuity_state="same_episode_candidate",
        )

    async def review(source_bundle: dict, _summary_md: str, _labels_md: str) -> str:
        ref = source_bundle["source_refs"][0]["uri"]
        return _approved_review_xml(package_id=source_bundle["package_id"], ref=ref, allowed_next="t3_intake")

    result = await build_t2_segment_package(
        data_root=tmp_path,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        t0_segment_id=first.segment_id,
        summary_agent=summary_agent,
        learning_brain=labels,
        memory_gate=review,
    )

    report = json.loads((result.staging_dir / "platform_gate_report.json").read_text(encoding="utf-8"))
    assert result.status == "held"
    assert any("continuity_state" in issue for issue in report["issues"])
    assert not result.package_dir.exists()


@pytest.mark.asyncio
async def test_episode_stitch_package_commits_without_learning_brain(tmp_path: Path) -> None:
    from app.memory.t2.segment_package import build_t2_episode_stitch_package, build_t2_segment_package

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    first = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="user_message",
        role="user",
        content="请创建 RWA 调研员。",
        source="web",
        data_root=tmp_path,
    )

    async def summary_agent(source_bundle: dict) -> str:
        return _summary_xml(source_bundle, status="rolling_checkpoint", segment_state="continuation")

    async def labels(source_bundle: dict, _summary_md: str) -> str:
        return _labels_xml(
            source_bundle,
            package_status="rolling_checkpoint",
            continuity_state="same_episode_candidate",
        )

    async def review(source_bundle: dict, _summary_md: str, _labels_md: str) -> str:
        ref = source_bundle["source_refs"][0]["uri"]
        return _approved_review_xml(package_id=source_bundle["package_id"], ref=ref, allowed_next="episode_stitching")

    t2_result = await build_t2_segment_package(
        data_root=tmp_path,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        t0_segment_id=first.segment_id,
        summary_agent=summary_agent,
        learning_brain=labels,
        memory_gate=review,
        package_id="t2pkg-rwa-1",
    )
    assert t2_result.status == "committed"

    calls: list[str] = []

    async def stitcher_agent(episode_bundle: dict) -> str:
        calls.append("stitcher")
        package_ref = episode_bundle["source_packages"][0]["package_id"]
        source_ref = episode_bundle["t0_source_refs"][0]
        return f"""# T2 Episode Synthesis

<episode_synthesis schema_version="t2.episode_synthesis.v1" episode_id="{episode_bundle["episode_id"]}" session_id="{episode_bundle["session_id"]}" status="closed">
  <source_packages><package_ref package_id="{package_ref}" t0_segment_id="{first.segment_id}" relationship="same_episode"/></source_packages>
  <source_refs><source_ref uri="{source_ref}"/></source_refs>
  <episode_summary><scenario>用户开始创建 RWA 调研员。</scenario></episode_summary>
  <continuity_decision relationship="same_episode" confidence="0.90"><reason>同一创建任务。</reason></continuity_decision>
  <promotion_hints><hint target="t3_intake" reason="closed episode"/></promotion_hints>
</episode_synthesis>
"""

    async def episode_gate(episode_bundle: dict, synthesis_md: str) -> str:
        calls.append("gate")
        source_ref = episode_bundle["t0_source_refs"][0]
        return f"""# T2 Episode Review

<episode_review schema_version="t2.episode_review.v1" episode_id="{episode_bundle["episode_id"]}" reviewer="memory_gate_agent">
  <decision>approved</decision>
  <allowed_next>t3_intake</allowed_next>
  <episode_review_rubric schema_version="t2.episode_review_rubric.v1">
    <score name="continuity_fidelity" value="0.90"/>
    <score name="source_ref_coverage" value="0.95"/>
    <score name="correction_quality" value="0.90"/>
    <score name="closure_quality" value="0.90"/>
    <score name="safety_scope" value="1.00"/>
    <review_score>0.90</review_score>
  </episode_review_rubric>
  <source_refs_checked><source_ref uri="{source_ref}"/></source_refs_checked>
</episode_review>
"""

    episode_result = await build_t2_episode_stitch_package(
        data_root=tmp_path,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        trigger_package_id="t2pkg-rwa-1",
        stitcher_agent=stitcher_agent,
        memory_gate=episode_gate,
        episode_id="episode-rwa",
    )

    assert calls == ["stitcher", "gate"]
    assert episode_result.status == "committed"
    assert (
        episode_result.package_dir
        == tmp_path / str(agent_id) / "memory" / "t2" / "sessions" / str(session_id) / "episodes" / "episode-rwa"
    )
    assert sorted(path.name for path in episode_result.package_dir.iterdir()) == [
        "manifest.json",
        "review.md",
        "synthesis.md",
    ]
    manifest = json.loads((episode_result.package_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "t2.episode-stitch.manifest.v1"
    assert manifest["prompts"]["stitcher_prompt_version"] == "t2.episode_stitcher.v1"
    assert manifest["prompts"]["review_prompt_version"] == "t2.episode_gate_review.v1"
    assert manifest["source_packages"] == ["t2pkg-rwa-1"]
    assert (t2_result.package_dir / "summary.md").exists()


@pytest.mark.asyncio
async def test_episode_bundle_includes_adjacent_t2_refs_by_default(tmp_path: Path) -> None:
    from app.memory.t0.ledger import seal_t0_session_segment
    from app.memory.t2.segment_package import build_t2_episode_stitch_package, build_t2_segment_package

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    first = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="user_message",
        role="user",
        content="我想先设计一个 RWA 调研员。",
        source="web",
        data_root=tmp_path,
    )
    seal_t0_session_segment(
        agent_id=agent_id,
        session_id=session_id,
        reason="test_segment_boundary",
        data_root=tmp_path,
    )
    second = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="user_message",
        role="user",
        content="继续刚才那个 RWA 调研员，把触发频率也定下来。",
        source="web",
        data_root=tmp_path,
    )

    async def complete_summary(source_bundle: dict) -> str:
        return _summary_xml(source_bundle)

    async def complete_labels(source_bundle: dict, _summary_md: str) -> str:
        return _labels_xml(source_bundle)

    async def complete_review(source_bundle: dict, _summary_md: str, _labels_md: str) -> str:
        return _approved_review_xml(package_id=source_bundle["package_id"], ref=source_bundle["source_refs"][0]["uri"])

    async def continuing_summary(source_bundle: dict) -> str:
        return _summary_xml(source_bundle, status="rolling_checkpoint", segment_state="continuation")

    async def continuing_labels(source_bundle: dict, _summary_md: str) -> str:
        return _labels_xml(
            source_bundle,
            package_status="rolling_checkpoint",
            continuity_state="needs_previous",
        )

    async def stitching_review(source_bundle: dict, _summary_md: str, _labels_md: str) -> str:
        return _approved_review_xml(
            package_id=source_bundle["package_id"],
            ref=source_bundle["source_refs"][0]["uri"],
            allowed_next="episode_stitching",
        )

    await build_t2_segment_package(
        data_root=tmp_path,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        t0_segment_id=first.segment_id,
        summary_agent=complete_summary,
        learning_brain=complete_labels,
        memory_gate=complete_review,
        package_id="t2pkg-prev",
    )
    await build_t2_segment_package(
        data_root=tmp_path,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        t0_segment_id=second.segment_id,
        summary_agent=continuing_summary,
        learning_brain=continuing_labels,
        memory_gate=stitching_review,
        package_id="t2pkg-current",
    )

    async def stitcher_agent(episode_bundle: dict) -> str:
        package_ids = [package["package_id"] for package in episode_bundle["source_packages"]]
        assert package_ids == ["t2pkg-current", "t2pkg-prev"]
        assert len(episode_bundle["t0_source_refs"]) == 2
        package_refs = "\n".join(
            f'<package_ref package_id="{package_id}" relationship="same_episode"/>' for package_id in package_ids
        )
        source_refs = "\n".join(f'<source_ref uri="{ref}"/>' for ref in episode_bundle["t0_source_refs"])
        return f"""<episode_synthesis schema_version="t2.episode_synthesis.v1" episode_id="{episode_bundle["episode_id"]}" session_id="{episode_bundle["session_id"]}" status="closed">
  <source_packages>{package_refs}</source_packages>
  <source_refs>{source_refs}</source_refs>
  <episode_summary><scenario>用户连续创建 RWA 调研员。</scenario></episode_summary>
</episode_synthesis>"""

    async def episode_gate(episode_bundle: dict, _synthesis_md: str) -> str:
        return f"""<episode_review schema_version="t2.episode_review.v1" episode_id="{episode_bundle["episode_id"]}">
  <decision>approved</decision>
  <allowed_next>t3_intake</allowed_next>
  <episode_review_rubric schema_version="t2.episode_review_rubric.v1">
    <score name="continuity_fidelity" value="0.90"/>
    <score name="source_ref_coverage" value="0.95"/>
    <score name="correction_quality" value="0.90"/>
    <score name="closure_quality" value="0.90"/>
    <score name="safety_scope" value="1.00"/>
    <review_score>0.90</review_score>
  </episode_review_rubric>
  <source_refs_checked><source_ref uri="{episode_bundle["t0_source_refs"][0]}"/></source_refs_checked>
</episode_review>"""

    result = await build_t2_episode_stitch_package(
        data_root=tmp_path,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        trigger_package_id="t2pkg-current",
        stitcher_agent=stitcher_agent,
        memory_gate=episode_gate,
        episode_id="episode-rwa-adjacent",
    )

    assert result.status == "committed"


@pytest.mark.asyncio
async def test_episode_stitch_package_holds_when_previous_package_is_different_lineage(tmp_path: Path) -> None:
    from app.memory.t0.ledger import seal_t0_session_segment
    from app.memory.t2.segment_package import build_t2_episode_stitch_package, build_t2_segment_package

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    first = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="user_message",
        role="user",
        content="原始分支里先讨论 RWA 调研员。",
        source="web",
        data_root=tmp_path,
    )
    seal_t0_session_segment(
        agent_id=agent_id,
        session_id=session_id,
        reason="test_segment_boundary",
        data_root=tmp_path,
    )
    second = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="user_message",
        role="user",
        content="rollback 后在新分支继续这个 RWA 调研员。",
        source="web",
        metadata={"branch_mode": "rewind", "source_session_id": "source-session-1"},
        data_root=tmp_path,
    )

    async def complete_summary(source_bundle: dict) -> str:
        return _summary_xml(source_bundle)

    async def complete_labels(source_bundle: dict, _summary_md: str) -> str:
        return _labels_xml(source_bundle)

    async def complete_review(source_bundle: dict, _summary_md: str, _labels_md: str) -> str:
        return _approved_review_xml(package_id=source_bundle["package_id"], ref=source_bundle["source_refs"][0]["uri"])

    async def continuing_summary(source_bundle: dict) -> str:
        return _summary_xml(source_bundle, status="rolling_checkpoint", segment_state="continuation")

    async def continuing_labels(source_bundle: dict, _summary_md: str) -> str:
        return _labels_xml(
            source_bundle,
            package_status="rolling_checkpoint",
            continuity_state="needs_previous",
        )

    async def stitching_review(source_bundle: dict, _summary_md: str, _labels_md: str) -> str:
        return _approved_review_xml(
            package_id=source_bundle["package_id"],
            ref=source_bundle["source_refs"][0]["uri"],
            allowed_next="episode_stitching",
        )

    await build_t2_segment_package(
        data_root=tmp_path,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        t0_segment_id=first.segment_id,
        summary_agent=complete_summary,
        learning_brain=complete_labels,
        memory_gate=complete_review,
        package_id="t2pkg-prev",
    )
    current = await build_t2_segment_package(
        data_root=tmp_path,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        t0_segment_id=second.segment_id,
        summary_agent=continuing_summary,
        learning_brain=continuing_labels,
        memory_gate=stitching_review,
        package_id="t2pkg-current",
        session_lineage={
            "root_session_id": str(session_id),
            "parent_session_id": "source-session-1",
            "branch_mode": "rewind",
            "source_session_id": "source-session-1",
            "anchor_event_id": "anchor-event-1",
            "anchor_sequence": 3,
        },
    )
    assert current.status == "committed"

    async def stitcher_agent(episode_bundle: dict) -> str:
        assert [package["package_id"] for package in episode_bundle["source_packages"]] == ["t2pkg-current"]
        assert episode_bundle["lineage_warnings"] == ["missing_compatible_adjacent_source"]
        source_ref = episode_bundle["t0_source_refs"][0]
        return f"""<episode_synthesis schema_version="t2.episode_synthesis.v1" episode_id="{episode_bundle["episode_id"]}" session_id="{episode_bundle["session_id"]}" status="closed">
  <source_packages><package_ref package_id="t2pkg-current" relationship="same_episode"/></source_packages>
  <source_refs><source_ref uri="{source_ref}"/></source_refs>
  <episode_summary><scenario>缺少同 lineage 前文，不能闭合。</scenario></episode_summary>
</episode_synthesis>"""

    async def episode_gate(episode_bundle: dict, _synthesis_md: str) -> str:
        return f"""<episode_review schema_version="t2.episode_review.v1" episode_id="{episode_bundle["episode_id"]}">
  <decision>approved</decision>
  <allowed_next>t3_intake</allowed_next>
  <episode_review_rubric schema_version="t2.episode_review_rubric.v1">
    <score name="continuity_fidelity" value="0.90"/>
    <score name="source_ref_coverage" value="0.95"/>
    <score name="correction_quality" value="0.90"/>
    <score name="closure_quality" value="0.90"/>
    <score name="safety_scope" value="1.00"/>
    <review_score>0.90</review_score>
  </episode_review_rubric>
</episode_review>"""

    result = await build_t2_episode_stitch_package(
        data_root=tmp_path,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        trigger_package_id="t2pkg-current",
        stitcher_agent=stitcher_agent,
        memory_gate=episode_gate,
        episode_id="episode-cross-lineage",
    )

    report = json.loads((result.staging_dir / "platform_gate_report.json").read_text(encoding="utf-8"))
    assert result.status == "held"
    assert any("missing compatible adjacent source package" in issue for issue in report["issues"])


@pytest.mark.asyncio
async def test_episode_stitch_package_holds_without_episode_review_rubric(tmp_path: Path) -> None:
    from app.memory.t2.segment_package import build_t2_episode_stitch_package, build_t2_segment_package

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    first = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="user_message",
        role="user",
        content="继续刚才的创建任务。",
        source="web",
        data_root=tmp_path,
    )

    async def summary_agent(source_bundle: dict) -> str:
        return _summary_xml(source_bundle, status="rolling_checkpoint", segment_state="continuation")

    async def labels(source_bundle: dict, _summary_md: str) -> str:
        return _labels_xml(source_bundle, package_status="rolling_checkpoint", continuity_state="needs_previous")

    async def review(source_bundle: dict, _summary_md: str, _labels_md: str) -> str:
        ref = source_bundle["source_refs"][0]["uri"]
        return _approved_review_xml(package_id=source_bundle["package_id"], ref=ref, allowed_next="episode_stitching")

    await build_t2_segment_package(
        data_root=tmp_path,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        t0_segment_id=first.segment_id,
        summary_agent=summary_agent,
        learning_brain=labels,
        memory_gate=review,
        package_id="t2pkg-open-1",
    )

    async def stitcher_agent(episode_bundle: dict) -> str:
        source_ref = episode_bundle["t0_source_refs"][0]
        return f"<episode_synthesis schema_version='t2.episode_synthesis.v1' episode_id='{episode_bundle['episode_id']}' status='open'><source_packages><package_ref package_id='t2pkg-open-1'/></source_packages><source_refs><source_ref uri='{source_ref}'/></source_refs></episode_synthesis>"

    async def bad_episode_gate(_episode_bundle: dict, _synthesis_md: str) -> str:
        return "<episode_review schema_version='t2.episode_review.v1'><decision>approved</decision><allowed_next>t3_intake</allowed_next></episode_review>"

    result = await build_t2_episode_stitch_package(
        data_root=tmp_path,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        trigger_package_id="t2pkg-open-1",
        stitcher_agent=stitcher_agent,
        memory_gate=bad_episode_gate,
        episode_id="episode-held",
    )

    report = json.loads((result.staging_dir / "platform_gate_report.json").read_text(encoding="utf-8"))
    assert result.status == "held"
    assert not result.package_dir.exists()
    assert any("episode_review_rubric" in issue for issue in report["issues"])


@pytest.mark.asyncio
async def test_build_with_llm_runs_review_role_for_low_risk_segments(monkeypatch, tmp_path: Path) -> None:
    from app.memory.t2 import segment_package

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    first = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="user_message",
        role="user",
        content="这个 session 需要被三个不同角色处理。",
        source="web",
        data_root=tmp_path,
    )
    phases: list[str] = []

    async def fake_model_config(_tenant_id):
        return {"provider": "fake", "model": "fake"}

    async def fake_run_agent(**kwargs):
        phases.append(kwargs["phase"])
        payload = kwargs["payload"]
        source_bundle = payload if kwargs["phase"] == "summary" else payload["source_bundle"]
        ref = source_bundle["source_refs"][0]["uri"]
        package_id = source_bundle["package_id"]
        if kwargs["phase"] == "summary":
            return _summary_xml(source_bundle)
        if kwargs["phase"] == "labels":
            return _labels_xml(source_bundle)
        return _approved_review_xml(package_id=package_id, ref=ref)

    monkeypatch.setattr("app.services.memory_service._get_summary_model_config", fake_model_config)
    monkeypatch.setattr(segment_package, "_run_t2_llm_agent", fake_run_agent)

    result = await segment_package.build_t2_segment_package_with_llm(
        data_root=tmp_path,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        t0_segment_id=first.segment_id,
    )

    assert result.status == "committed"
    assert phases == ["summary", "labels", "review"]
    assert (result.package_dir / "summary.md").exists()
    assert (result.package_dir / "labels.md").exists()
    assert (result.package_dir / "review.md").exists()
    assert (result.package_dir / "manifest.json").exists()


@pytest.mark.asyncio
async def test_build_with_llm_runs_review_role_for_high_risk_segments(monkeypatch, tmp_path: Path) -> None:
    from app.memory.t2 import segment_package

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    first = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="user_message",
        role="user",
        content="这个 session 涉及跨主体权限边界，需要独立 reviewer。",
        source="web",
        data_root=tmp_path,
    )
    phases: list[str] = []

    async def fake_model_config(_tenant_id):
        return {"provider": "fake", "model": "fake"}

    async def fake_run_agent(**kwargs):
        phases.append(kwargs["phase"])
        payload = kwargs["payload"]
        source_bundle = payload if kwargs["phase"] == "summary" else payload["source_bundle"]
        ref = source_bundle["source_refs"][0]["uri"]
        package_id = source_bundle["package_id"]
        if kwargs["phase"] == "summary":
            return _summary_xml(source_bundle)
        if kwargs["phase"] == "labels":
            return f"""# T2 Segment Labels

<t2_labels schema_version="t2.labels.v1" package_id="{package_id}" session_id="{source_bundle["session_id"]}" t2_segment_id="{source_bundle["t0_segment_id"]}">
  <control_metadata>
    <source_integrity>complete</source_integrity>
    <sensitivity>PL3</sensitivity>
    <principal_scope>company</principal_scope>
    <package_status>closed</package_status>
    <confidence>0.95</confidence>
    <continuity_state>standalone</continuity_state>
    <systems><system>memory</system></systems>
    <risk_flags><risk_flag>cross_principal</risk_flag></risk_flags>
  </control_metadata>
  <event_labels>
    <event_label event_ref="evt-1">
      <event_type>instruction</event_type>
      <memory_domain>permission_memory</memory_domain>
      <outcome>accepted</outcome>
      <actionability>t3_candidate</actionability>
      <stability>stable</stability>
      <completeness>closed</completeness>
      <salience>high</salience>
      <source_refs><source_ref uri="{ref}"/></source_refs>
    </event_label>
  </event_labels>
</t2_labels>
"""
        return _approved_review_xml(package_id=package_id, ref=ref)

    monkeypatch.setattr("app.services.memory_service._get_summary_model_config", fake_model_config)
    monkeypatch.setattr(segment_package, "_run_t2_llm_agent", fake_run_agent)

    result = await segment_package.build_t2_segment_package_with_llm(
        data_root=tmp_path,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        t0_segment_id=first.segment_id,
    )

    assert result.status == "committed"
    assert phases == ["summary", "labels", "review"]
    manifest = json.loads((result.package_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["review_mode"] == "independent_gate"


@pytest.mark.asyncio
async def test_build_with_llm_holds_without_model_config(monkeypatch, tmp_path: Path) -> None:
    from app.memory.t2.segment_package import build_t2_segment_package_with_llm

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    first = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="user_message",
        role="user",
        content="没有模型配置时不能机械总结。",
        source="web",
        data_root=tmp_path,
    )

    async def no_model_config(_tenant_id):
        return None

    monkeypatch.setattr("app.services.memory_service._get_summary_model_config", no_model_config)

    result = await build_t2_segment_package_with_llm(
        data_root=tmp_path,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        t0_segment_id=first.segment_id,
    )

    assert result.status == "held"
    assert not result.package_dir.exists()
    assert (result.staging_dir / "source_bundle.json").exists()
    report = json.loads((result.staging_dir / "platform_gate_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "held"
    assert "no summary model config" in report["issues"][0]
