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


def _approved_review_xml(*, package_id: str | None, ref: str) -> str:
    package_attr = f' package_id="{package_id}"' if package_id else ""
    return f"""<t2_review schema_version="t2.review.v1"{package_attr} reviewer="memory_gate_agent">
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
  <evidence_coverage>complete</evidence_coverage>
  <hallucination_risk>low</hallucination_risk>
  <label_quality>pass</label_quality>
  <sensitivity_result>pass</sensitivity_result>
  <issues/>
  <required_changes/>
  <source_refs_checked><source_ref uri="{ref}"/></source_refs_checked>
</t2_review>"""


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
        ref = source_bundle["source_refs"][0]
        return f"""# T2 Segment Summary

<t2_summary schema_version="t2.summary.v1" package_id="{source_bundle["package_id"]}" session_id="{source_bundle["session_id"]}" t0_segment_id="{source_bundle["t0_segment_id"]}" status="closed">
  <source_refs>
    <source_ref uri="{ref["uri"]}" path="{ref["path"]}" sha256="{ref["sha256"]}"/>
  </source_refs>
  <scenario>
    <title>架构改造前先讨论</title>
    <user_cues>
      <cue>先讨论，确认后再改</cue>
    </user_cues>
    <context>用户明确要求架构改造不要直接写。</context>
  </scenario>
  <events>
    <event id="evt-1" type="instruction" salience="high">
      <summary>用户要求架构改造先讨论，再落文档或代码。</summary>
      <evidence_refs><source_ref uri="{ref["uri"]}"/></evidence_refs>
    </event>
  </events>
  <facts>
    <fact evidence_strength="source_backed">用户偏好讨论优先于直接修改。</fact>
  </facts>
  <decisions>
    <decision status="accepted">架构改造默认先讨论边界。</decision>
  </decisions>
  <corrections/>
  <method_trace>
    <step>先确认边界，再写文档或代码。</step>
  </method_trace>
  <artifacts/>
  <open_questions/>
  <short_term_carryover/>
  <promotion_hints>
    <hint target="t3_candidate" reason="stable explicit user preference"/>
  </promotion_hints>
</t2_summary>
"""

    async def learning_brain(source_bundle: dict, summary_md: str) -> str:
        ref = source_bundle["source_refs"][0]["uri"]
        return f"""# T2 Segment Labels

<t2_labels schema_version="t2.labels.v1" package_id="{source_bundle["package_id"]}" session_id="{source_bundle["session_id"]}" t2_segment_id="{source_bundle["t0_segment_id"]}">
  <control_metadata>
    <source_integrity>complete</source_integrity>
    <sensitivity>PL1</sensitivity>
    <principal_scope>direct_owner</principal_scope>
    <package_status>closed</package_status>
    <confidence>0.95</confidence>
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
      <completeness>closed</completeness>
      <salience>high</salience>
      <cue_terms><cue>先讨论</cue></cue_terms>
      <subjects><project>hive-memory</project></subjects>
      <source_refs><source_ref uri="{ref}"/></source_refs>
    </event_label>
  </event_labels>
</t2_labels>
"""

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

    package_dir = tmp_path / str(agent_id) / "memory" / "sessions" / str(session_id) / "segments" / first.segment_id
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

    manifest = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "t2.segment-package.manifest.v1"
    assert manifest["package_status"] == "reviewed"
    assert manifest["source_refs"][0].startswith(f"t0://session/{session_id}/segment/{first.segment_id}#seq=1..")
    assert manifest["files"]["summary.md"]["sha256"]
    assert manifest["prompts"]["summary_prompt_version"] == "t2.summary_agent.v1"
    assert manifest["prompts"]["labels_prompt_version"] == "t2.learning_brain_labels.v1"
    assert manifest["prompts"]["review_prompt_version"] == "t2.memory_gate_review.v1"

    source_bundle_path = (
        tmp_path / str(agent_id) / "memory" / ".staging" / "t2_jobs" / result.job_id / "source_bundle.json"
    )
    source_bundle = json.loads(source_bundle_path.read_text(encoding="utf-8"))
    assert source_bundle["schema_version"] == "t2.source_bundle.v1"
    assert source_bundle["source_refs"][0]["path"].endswith(
        f"memory/t0/sessions/{session_id}/segments/{first.segment_id}/source.md"
    )
    assert source_bundle["t0_events"][0]["content"] == "以后架构改造先讨论，确认后再改文档。"

    assert _assert_xml_block(package_dir / "summary.md", "t2_summary").attrib["status"] == "closed"
    assert _assert_xml_block(package_dir / "labels.md", "t2_labels").findtext(".//confidence") == "0.95"
    assert _assert_xml_block(package_dir / "review.md", "t2_review").findtext("decision") == "approved"


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
        ref = source_bundle["source_refs"][0]
        return f"<t2_summary schema_version='t2.summary.v1'><source_refs><source_ref uri='{ref['uri']}'/></source_refs></t2_summary>"

    async def labels(source_bundle: dict, _summary_md: str) -> str:
        ref = source_bundle["source_refs"][0]["uri"]
        return f"<t2_labels schema_version='t2.labels.v1'><source_refs><source_ref uri='{ref}'/></source_refs></t2_labels>"

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

    package_dir = tmp_path / str(agent_id) / "memory" / "sessions" / str(session_id) / "segments" / first.segment_id
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
        ref = source_bundle["source_refs"][0]["uri"]
        return f"<t2_summary schema_version='t2.summary.v1' status='closed'><source_refs><source_ref uri='{ref}'/></source_refs></t2_summary>"

    async def labels(source_bundle: dict, _summary_md: str) -> str:
        ref = source_bundle["source_refs"][0]["uri"]
        return f"""<t2_labels schema_version='t2.labels.v1'>
  <control_metadata><package_status>closed</package_status></control_metadata>
  <source_refs><source_ref uri='{ref}'/></source_refs>
</t2_labels>"""

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
async def test_build_with_llm_runs_three_agent_roles(monkeypatch, tmp_path: Path) -> None:
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
            return f"<t2_summary schema_version='t2.summary.v1' package_id='{package_id}'><source_refs><source_ref uri='{ref}'/></source_refs></t2_summary>"
        if kwargs["phase"] == "labels":
            return f"<t2_labels schema_version='t2.labels.v1' package_id='{package_id}'><source_refs><source_ref uri='{ref}'/></source_refs></t2_labels>"
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
