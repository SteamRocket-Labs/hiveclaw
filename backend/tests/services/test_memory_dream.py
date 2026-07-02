from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4


def _write_reviewed_t2_package(
    root: Path,
    *,
    agent_id,
    session_id: str,
    segment_id: str,
    package_id: str,
    claim: str,
    base: str = "memory/t2/sessions",
) -> Path:
    package_dir = root / str(agent_id) / base / session_id / "segments" / segment_id
    package_dir.mkdir(parents=True, exist_ok=True)
    source_ref = f"t0://session/{session_id}/segment/{segment_id}#seq=1..2"
    summary = f"""# T2 Segment Summary

<t2_summary schema_version="t2.summary.v1" package_id="{package_id}" session_id="{session_id}" t0_segment_id="{segment_id}" status="closed">
  <source_refs><source_ref uri="{source_ref}" path="memory/t0/sessions/{session_id}/segments/{segment_id}/source.md" sha256="abc"/></source_refs>
  <segment_state value="complete"><reason>closed test segment</reason></segment_state>
  <scenario><title>Memory Dream Test</title><context>{claim}</context></scenario>
  <continuity><open_threads/></continuity>
</t2_summary>
"""
    labels = f"""# T2 Segment Labels

<t2_labels schema_version="t2.labels.v1" package_id="{package_id}" session_id="{session_id}" t2_segment_id="{segment_id}">
  <control_metadata>
    <source_integrity>complete</source_integrity>
    <sensitivity>PL1</sensitivity>
    <principal_scope>direct_owner</principal_scope>
    <package_status>closed</package_status>
    <confidence>0.95</confidence>
    <continuity_state>standalone</continuity_state>
    <systems><system>memory</system></systems>
    <risk_flags/>
  </control_metadata>
  <event_labels/>
</t2_labels>
"""
    review = f"""# T2 Segment Review

<t2_review schema_version="t2.review.v1" reviewer="memory_gate_agent">
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
  <source_refs_checked><source_ref uri="{source_ref}"/></source_refs_checked>
</t2_review>
"""
    manifest = {
        "schema_version": "t2.segment-package.manifest.v1",
        "package_id": package_id,
        "agent_id": str(agent_id),
        "session_id": session_id,
        "t0_segment_id": segment_id,
        "source_refs": [source_ref],
        "package_status": "reviewed",
        "files": {
            "summary.md": {"sha256": "summary"},
            "labels.md": {"sha256": "labels"},
            "review.md": {"sha256": "review"},
        },
    }
    (package_dir / "summary.md").write_text(summary, encoding="utf-8")
    (package_dir / "labels.md").write_text(labels, encoding="utf-8")
    (package_dir / "review.md").write_text(review, encoding="utf-8")
    (package_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return package_dir


def _write_reviewed_episode_stitch_package(
    root: Path,
    *,
    agent_id,
    session_id: str,
    episode_id: str,
    package_id: str,
    claim: str,
    base: str = "memory/t2/sessions",
) -> Path:
    package_dir = root / str(agent_id) / base / session_id / "episodes" / episode_id
    package_dir.mkdir(parents=True, exist_ok=True)
    source_ref = f"t0://session/{session_id}/segment/segment-1#seq=1..4"
    synthesis = f"""# Episode Synthesis

<episode_synthesis schema_version="t2.episode_synthesis.v1" episode_id="{episode_id}" session_id="{session_id}" status="closed">
  <source_packages><package_ref package_id="{package_id}" t0_segment_id="segment-1" relationship="same_episode"/></source_packages>
  <source_refs><source_ref uri="{source_ref}"/></source_refs>
  <episode_summary><scenario>{claim}</scenario></episode_summary>
</episode_synthesis>
"""
    review = f"""# Episode Review

<episode_review schema_version="t2.episode_review.v1" episode_id="{episode_id}" reviewer="memory_gate_agent">
  <decision>approved</decision>
  <allowed_next>t3_intake</allowed_next>
  <episode_review_rubric schema_version="t2.episode_review_rubric.v1">
    <score name="continuity_fidelity" value="0.95"/>
    <score name="source_ref_coverage" value="0.95"/>
    <score name="correction_quality" value="0.90"/>
    <score name="closure_quality" value="0.90"/>
    <score name="safety_scope" value="1.00"/>
    <review_score>0.95</review_score>
  </episode_review_rubric>
  <source_refs_checked><source_ref uri="{source_ref}"/></source_refs_checked>
</episode_review>
"""
    manifest = {
        "schema_version": "t2.episode-stitch.manifest.v1",
        "episode_id": episode_id,
        "package_id": package_id,
        "agent_id": str(agent_id),
        "session_id": session_id,
        "source_refs": [source_ref],
        "source_packages": [package_id],
        "package_status": "reviewed",
        "files": {
            "synthesis.md": {"sha256": "synthesis"},
            "review.md": {"sha256": "review"},
        },
    }
    (package_dir / "synthesis.md").write_text(synthesis, encoding="utf-8")
    (package_dir / "review.md").write_text(review, encoding="utf-8")
    (package_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return package_dir


def test_prepare_memory_dream_workspace_writes_diff_without_touching_t3(tmp_path: Path) -> None:
    from app.services.memory_dream import prepare_memory_dream_workspace

    agent_id = uuid4()
    package_dir = _write_reviewed_t2_package(
        tmp_path,
        agent_id=agent_id,
        session_id="session-1",
        segment_id="segment-1",
        package_id="pkg-1",
        claim="User wants tests before implementation.",
    )
    t3_user = tmp_path / str(agent_id) / "memory" / "t3" / "user.md"
    t3_user.parent.mkdir(parents=True, exist_ok=True)
    t3_user.write_text("# T3 User\n\n", encoding="utf-8")

    result = prepare_memory_dream_workspace(agent_id=agent_id, data_root=tmp_path)

    assert result.status == "changed"
    assert result.workspace_dir == tmp_path / str(agent_id) / "memory" / ".staging" / "dream_workspace"
    assert result.selected_package_dirs == (package_dir,)
    assert result.diff_path.exists()
    assert "pkg-1" in result.diff_path.read_text(encoding="utf-8")
    assert "User wants tests before implementation." in (result.workspace_dir / "raw_t2_inputs.md").read_text(
        encoding="utf-8"
    )
    assert (result.workspace_dir / "rollout_summaries" / "pkg-1.md").exists()
    assert t3_user.read_text(encoding="utf-8") == "# T3 User\n\n"


def test_memory_dream_workspace_baseline_suppresses_unchanged_inputs(tmp_path: Path) -> None:
    from app.services.memory_dream import finalize_memory_dream_workspace, prepare_memory_dream_workspace

    agent_id = uuid4()
    _write_reviewed_t2_package(
        tmp_path,
        agent_id=agent_id,
        session_id="session-1",
        segment_id="segment-1",
        package_id="pkg-1",
        claim="User wants tests before implementation.",
    )

    first = prepare_memory_dream_workspace(agent_id=agent_id, data_root=tmp_path)
    finalize_memory_dream_workspace(first)
    second = prepare_memory_dream_workspace(agent_id=agent_id, data_root=tmp_path)

    assert first.status == "changed"
    assert second.status == "no_changes"
    assert not second.diff_path.exists()


def test_prepare_memory_dream_workspace_reads_legacy_t2_packages(tmp_path: Path) -> None:
    from app.services.memory_dream import prepare_memory_dream_workspace

    agent_id = uuid4()
    package_dir = _write_reviewed_t2_package(
        tmp_path,
        agent_id=agent_id,
        session_id="session-legacy",
        segment_id="segment-legacy",
        package_id="pkg-legacy",
        claim="Legacy package remains importable.",
        base="memory/sessions",
    )

    result = prepare_memory_dream_workspace(agent_id=agent_id, data_root=tmp_path)

    assert result.status == "changed"
    assert result.selected_package_dirs == (package_dir,)
    assert "pkg-legacy" in result.diff_path.read_text(encoding="utf-8")


def test_prepare_memory_dream_workspace_reads_episode_stitch_packages(tmp_path: Path) -> None:
    from app.services.memory_dream import prepare_memory_dream_workspace

    agent_id = uuid4()
    package_dir = _write_reviewed_episode_stitch_package(
        tmp_path,
        agent_id=agent_id,
        session_id="session-episode",
        episode_id="episode-1",
        package_id="episode-pkg-1",
        claim="Disconnected fragments have been stitched into one episode.",
    )

    result = prepare_memory_dream_workspace(agent_id=agent_id, data_root=tmp_path)

    assert result.status == "changed"
    assert result.selected_package_dirs == (package_dir,)
    raw_inputs = (result.workspace_dir / "raw_t2_inputs.md").read_text(encoding="utf-8")
    assert "episode-pkg-1" in raw_inputs
    assert "Disconnected fragments have been stitched into one episode." in raw_inputs


def test_run_memory_dream_stages_t3_batch_but_does_not_commit_t3(tmp_path: Path) -> None:
    from app.services.memory_dream import finalize_memory_dream_workspace, run_memory_dream

    agent_id = uuid4()
    package_dir = _write_reviewed_t2_package(
        tmp_path,
        agent_id=agent_id,
        session_id="session-1",
        segment_id="segment-1",
        package_id="pkg-1",
        claim="User wants tests before implementation.",
    )
    t3_user = tmp_path / str(agent_id) / "memory" / "t3" / "user.md"
    t3_user.parent.mkdir(parents=True, exist_ok=True)
    t3_user.write_text("# T3 User\n\n", encoding="utf-8")

    result = run_memory_dream(agent_id=agent_id, data_root=tmp_path)

    assert result.status == "staged"
    assert result.workspace_result.selected_package_dirs == (package_dir,)
    assert result.t3_batch_result is not None
    assert (result.t3_batch_result.job_dir / "source_bundle.json").exists()
    assert (result.workspace_result.workspace_dir / "phase2_workspace_diff.md").exists()
    assert t3_user.read_text(encoding="utf-8") == "# T3 User\n\n"

    finalize_memory_dream_workspace(result.workspace_result)
    assert run_memory_dream(agent_id=agent_id, data_root=tmp_path).status == "no_changes"


def test_run_memory_dream_stages_episode_stitch_packages(tmp_path: Path) -> None:
    from app.services.memory_dream import run_memory_dream

    agent_id = uuid4()
    package_dir = _write_reviewed_episode_stitch_package(
        tmp_path,
        agent_id=agent_id,
        session_id="session-episode",
        episode_id="episode-1",
        package_id="episode-pkg-1",
        claim="Disconnected fragments have been stitched into one episode.",
    )

    result = run_memory_dream(agent_id=agent_id, data_root=tmp_path)

    assert result.status == "staged"
    assert result.workspace_result.selected_package_dirs == (package_dir,)
    assert result.t3_batch_result is not None
    assert (result.t3_batch_result.job_dir / "source_bundle.json").exists()
    # C7 cutover: no flat-T3 skeleton exists; staging output is the contract.
    assert not (tmp_path / str(agent_id) / "memory" / "t3" / "episodes.md").exists()


def test_run_dream_reports_memory_dream_lane_when_t3_is_empty(tmp_path: Path, monkeypatch) -> None:
    import asyncio

    from app.services.auto_dream import run_dream

    agent_id = uuid4()
    tenant_id = uuid4()
    _write_reviewed_t2_package(
        tmp_path,
        agent_id=agent_id,
        session_id="session-1",
        segment_id="segment-1",
        package_id="pkg-1",
        claim="User wants tests before implementation.",
    )

    class Settings:
        AGENT_DATA_DIR = str(tmp_path)

    monkeypatch.setattr("app.services.auto_dream.get_settings", lambda: Settings())
    monkeypatch.setattr("app.services.memory_dream.get_settings", lambda: Settings())

    result = asyncio.run(run_dream(agent_id, tenant_id))

    assert result["consolidated"] == 0
    assert result["memory_dream"]["status"] == "staged"
    assert result["memory_dream"]["t3_batch_job_id"]
