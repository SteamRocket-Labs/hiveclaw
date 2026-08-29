"""C9-1 red tests: T2 held/failed job sweep (spec §6.2.1).

Held/failed T2 jobs are valid terminal states today but nothing ever picks
them back up — experience silently never enters memory. The sweep contract:

- ``sweep_stale_t2_jobs`` (sync, zero LLM): crash recovery only — stale
  queued/running job manifests are normalized to held. Startup entry point.
- ``sweep_t2_jobs`` (async): stale recovery + bounded retry of held/failed
  jobs through the canonical ``run_t2_segment_package_job`` (same stable
  job_id → idempotent). Heartbeat entry point.
- Retry state lives in the job manifest (``retry_count``/``last_retry_at``);
  exhausting ``max_retries`` emits ONE audit alert and marks the manifest so
  repeated sweeps do not re-alert.
- Every sweep writes an auditable report to ``memory/control/t2_job_sweep.json``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.memory.t0.ledger import append_t0_session_event


# --- compliant LLM-output fixtures (same shapes as test_t2_segment_package_builder) ---


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
  <continuity_result>standalone</continuity_result>
  <sensitivity_result>pass</sensitivity_result>
  <issues/>
  <required_changes/>
  <source_refs_checked><source_ref uri="{ref}"/></source_refs_checked>
</t2_review>"""


def _summary_xml(source_bundle: dict) -> str:
    ref = source_bundle["source_refs"][0]
    return f"""# T2 Segment Summary

<t2_summary schema_version="t2.summary.v1" package_id="{source_bundle["package_id"]}" session_id="{source_bundle["session_id"]}" t0_segment_id="{source_bundle["t0_segment_id"]}" status="closed">
  <source_refs>
    <source_ref uri="{ref["uri"]}" path="{ref["path"]}" sha256="{ref["sha256"]}"/>
  </source_refs>
  <segment_state value="complete">
    <reason>测试片段状态。</reason>
  </segment_state>
  <scenario><title>测试场景</title><context>用于 T2 job sweep 测试。</context></scenario>
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


def _labels_xml(source_bundle: dict) -> str:
    ref = source_bundle["source_refs"][0]["uri"]
    return f"""# T2 Segment Labels

<t2_labels schema_version="t2.labels.v1" package_id="{source_bundle["package_id"]}" session_id="{source_bundle["session_id"]}" t2_segment_id="{source_bundle["t0_segment_id"]}">
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
  <event_labels>
    <event_label event_ref="evt-1">
      <event_type>instruction</event_type>
      <memory_domain>preference_memory</memory_domain>
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


def _patch_working_llm(monkeypatch) -> list[str]:
    """Route T2 LLM phases to compliant fixture outputs; return call log."""
    from app.memory.t2 import segment_package

    phases: list[str] = []

    async def fake_model_config(_tenant_id):
        return {"provider": "fake", "model": "fake"}

    async def fake_run_agent(**kwargs):
        phases.append(kwargs["phase"])
        payload = kwargs["payload"]
        source_bundle = payload if kwargs["phase"] == "summary" else payload["source_bundle"]
        ref = source_bundle["source_refs"][0]["uri"]
        if kwargs["phase"] == "summary":
            return _summary_xml(source_bundle)
        if kwargs["phase"] == "labels":
            return _labels_xml(source_bundle)
        return _approved_review_xml(package_id=source_bundle["package_id"], ref=ref)

    monkeypatch.setattr("app.services.memory_service._get_summary_model_config", fake_model_config)
    monkeypatch.setattr(segment_package, "_run_t2_llm_agent", fake_run_agent)
    return phases


def _patch_missing_model_config(monkeypatch) -> None:
    async def no_model_config(_tenant_id):
        return None

    monkeypatch.setattr("app.services.memory_service._get_summary_model_config", no_model_config)


def _capture_audit_alerts(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    async def fake_write_audit_log(action: str, payload: dict | None = None, **kwargs):
        calls.append({"action": action, "payload": payload or {}, **kwargs})

    monkeypatch.setattr("app.services.audit_logger.write_audit_log", fake_write_audit_log)
    return calls


async def _make_held_job(*, tmp_path: Path, monkeypatch) -> tuple[dict, Path]:
    """Produce a real held job via the canonical runner (no model config)."""
    from app.memory.t2.segment_package import run_t2_segment_package_job

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    event = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="user_message",
        role="user",
        content="held job 场景：模型配置缺失时段包必须 hold。",
        source="web",
        data_root=tmp_path,
    )
    _patch_missing_model_config(monkeypatch)
    result = await run_t2_segment_package_job(
        data_root=tmp_path,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        t0_segment_id=event.segment_id,
    )
    assert result.status == "held"
    manifest_path = result.staging_dir / "job_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return manifest, manifest_path


def _jobs_dir(tmp_path: Path, agent_id) -> Path:
    return tmp_path / str(agent_id) / "memory" / ".staging" / "t2_jobs"


def _write_manifest(manifest_path: Path, manifest: dict) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _stale_manifest(*, agent_id, status: str, hours_ago: float = 2.0) -> dict:
    stamp = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
    return {
        "schema_version": "t2.segment-package-job.v1",
        "job_id": f"t2job-stale-{status}",
        "package_id": "t2pkg-stale",
        "agent_id": str(agent_id),
        "tenant_id": str(uuid4()),
        "session_id": str(uuid4()),
        "t0_segment_id": "seg-stale",
        "package_dir": "memory/t2/sessions/x/segments/seg-stale",
        "staging_dir": f"memory/.staging/t2_jobs/t2job-stale-{status}",
        "created_at": stamp,
        "updated_at": stamp,
        "status": status,
        "issues": [],
    }


# --- crash recovery (sync, zero LLM) ---


@pytest.mark.parametrize("status", ["queued", "running"])
def test_sweep_recovers_stale_inflight_job_to_held(tmp_path: Path, status: str) -> None:
    from app.memory.t2.job_sweep import sweep_stale_t2_jobs

    agent_id = uuid4()
    manifest = _stale_manifest(agent_id=agent_id, status=status)
    manifest_path = _jobs_dir(tmp_path, agent_id) / manifest["job_id"] / "job_manifest.json"
    _write_manifest(manifest_path, manifest)

    report = sweep_stale_t2_jobs(agent_id=agent_id, data_root=tmp_path)

    updated = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert updated["status"] == "held"
    assert any("crash" in issue for issue in updated["issues"])
    assert updated.get("recovered_from") == status
    assert int(updated.get("retry_count") or 0) == 0
    assert report.recovered_stale == (manifest["job_id"],)
    assert report.scanned == 1


def test_sweep_leaves_fresh_inflight_job_alone(tmp_path: Path) -> None:
    from app.memory.t2.job_sweep import sweep_stale_t2_jobs

    agent_id = uuid4()
    manifest = _stale_manifest(agent_id=agent_id, status="running", hours_ago=0.0)
    manifest_path = _jobs_dir(tmp_path, agent_id) / manifest["job_id"] / "job_manifest.json"
    _write_manifest(manifest_path, manifest)

    report = sweep_stale_t2_jobs(agent_id=agent_id, data_root=tmp_path)

    assert json.loads(manifest_path.read_text(encoding="utf-8"))["status"] == "running"
    assert report.recovered_stale == ()


def test_stale_sweep_never_retries_held_jobs(tmp_path: Path) -> None:
    """Startup entry point is state normalization only — zero LLM retries."""
    from app.memory.t2.job_sweep import sweep_stale_t2_jobs

    agent_id = uuid4()
    manifest = _stale_manifest(agent_id=agent_id, status="held")
    manifest["job_id"] = "t2job-held-untouched"
    manifest_path = _jobs_dir(tmp_path, agent_id) / manifest["job_id"] / "job_manifest.json"
    _write_manifest(manifest_path, manifest)

    report = sweep_stale_t2_jobs(agent_id=agent_id, data_root=tmp_path)

    updated = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert updated["status"] == "held"
    assert int(updated.get("retry_count") or 0) == 0
    assert report.retried == ()
    assert report.recovered_stale == ()


def test_sweep_all_agents_recovers_stale_jobs_across_agents(tmp_path: Path) -> None:
    from app.memory.t2.job_sweep import sweep_all_agents_stale_t2_jobs

    agent_a, agent_b = uuid4(), uuid4()
    for agent_id in (agent_a, agent_b):
        manifest = _stale_manifest(agent_id=agent_id, status="running")
        manifest_path = _jobs_dir(tmp_path, agent_id) / manifest["job_id"] / "job_manifest.json"
        _write_manifest(manifest_path, manifest)
    (tmp_path / "not-an-agent.txt").write_text("noise", encoding="utf-8")

    reports = sweep_all_agents_stale_t2_jobs(data_root=tmp_path)

    assert sorted(report.agent_id for report in reports) == sorted(str(a) for a in (agent_a, agent_b))
    for agent_id in (agent_a, agent_b):
        manifest_path = _jobs_dir(tmp_path, agent_id) / "t2job-stale-running" / "job_manifest.json"
        assert json.loads(manifest_path.read_text(encoding="utf-8"))["status"] == "held"


# --- bounded retry (async, heartbeat cadence) ---


@pytest.mark.asyncio
async def test_sweep_retries_held_job_and_commits_when_llm_recovers(tmp_path: Path, monkeypatch) -> None:
    from app.memory.t2.job_sweep import sweep_t2_jobs

    manifest, manifest_path = await _make_held_job(tmp_path=tmp_path, monkeypatch=monkeypatch)
    agent_id = manifest["agent_id"]

    phases = _patch_working_llm(monkeypatch)
    report = await sweep_t2_jobs(agent_id=agent_id, data_root=tmp_path)

    updated = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert updated["status"] == "committed"
    assert int(updated["retry_count"]) == 1
    assert updated["last_retry_at"]
    assert phases == ["summary", "labels", "review"]
    assert report.retried == (manifest["job_id"],)
    assert report.committed == (manifest["job_id"],)
    package_dir = (
        tmp_path
        / agent_id
        / "memory"
        / "t2"
        / "sessions"
        / manifest["session_id"]
        / "segments"
        / manifest["t0_segment_id"]
    )
    assert (package_dir / "summary.md").exists()
    assert (package_dir / "manifest.json").exists()
    committed_manifest = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
    assert committed_manifest["lineage"]["root_session_id"] == manifest["session_id"]


@pytest.mark.asyncio
async def test_sweep_retry_failure_keeps_held_and_increments_retry_count(tmp_path: Path, monkeypatch) -> None:
    from app.memory.t2.job_sweep import sweep_t2_jobs

    manifest, manifest_path = await _make_held_job(tmp_path=tmp_path, monkeypatch=monkeypatch)
    agent_id = manifest["agent_id"]

    first = await sweep_t2_jobs(agent_id=agent_id, data_root=tmp_path)
    second = await sweep_t2_jobs(agent_id=agent_id, data_root=tmp_path)

    updated = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert updated["status"] == "held"
    assert int(updated["retry_count"]) == 2
    assert first.still_held == (manifest["job_id"],)
    assert second.still_held == (manifest["job_id"],)


@pytest.mark.asyncio
async def test_sweep_converts_legacy_non_semantic_hold_to_terminal_not_applicable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.memory.t2 import job_sweep, segment_package

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    mechanical = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="run.completed",
        content="",
        source="runtime_control",
        data_root=tmp_path,
    )
    append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="segment_boundary",
        role="system",
        content="session_idle",
        source="runtime_control",
        data_root=tmp_path,
    )
    queued = segment_package.enqueue_t2_segment_package_job(
        data_root=tmp_path,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        t0_segment_id=mechanical.segment_id,
    )
    manifest_path = queued.staging_dir / "job_manifest.json"
    legacy_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    legacy_manifest.update(
        {
            "status": "held",
            "package_status": "held",
            "retry_count": 2,
            "issues": ["T0->T2 package build failed: ValueError: no semantic T0 events"],
        }
    )
    _write_manifest(manifest_path, legacy_manifest)
    model_calls: list[object] = []

    async def fake_model_config(actual_tenant_id):
        model_calls.append(actual_tenant_id)
        return {"provider": "fake", "model": "fake"}

    monkeypatch.setattr("app.services.memory_service._get_summary_model_config", fake_model_config)

    report = await job_sweep.sweep_t2_jobs(
        agent_id=agent_id,
        tenant_id=tenant_id,
        data_root=tmp_path,
    )

    updated = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert updated["status"] == "not_applicable"
    assert updated["package_status"] == "not_applicable"
    assert updated["reason_code"] == "no_semantic_t0_events"
    assert updated["retry_count"] == 3
    assert updated["issues"] == []
    assert model_calls == []
    assert report.retried == (queued.job_id,)
    assert report.not_applicable == (queued.job_id,)
    assert report.still_held == ()
    assert report.exhausted == ()
    control = json.loads(
        (tmp_path / str(agent_id) / "memory" / "control" / "t2_job_sweep.json").read_text(encoding="utf-8")
    )
    assert control["not_applicable"] == [queued.job_id]

    second_report = await job_sweep.sweep_t2_jobs(
        agent_id=agent_id,
        tenant_id=tenant_id,
        data_root=tmp_path,
    )
    unchanged = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert unchanged["retry_count"] == 3
    assert second_report.retried == ()
    assert second_report.not_applicable == (queued.job_id,)


@pytest.mark.asyncio
async def test_sweep_retries_failed_jobs_too(tmp_path: Path, monkeypatch) -> None:
    from app.memory.t2.job_sweep import sweep_t2_jobs

    manifest, manifest_path = await _make_held_job(tmp_path=tmp_path, monkeypatch=monkeypatch)
    agent_id = manifest["agent_id"]
    manifest["status"] = "failed"
    manifest["issues"] = ["T0->T2 job failed before package builder terminal state: RuntimeError: boom"]
    _write_manifest(manifest_path, manifest)

    _patch_working_llm(monkeypatch)
    report = await sweep_t2_jobs(agent_id=agent_id, data_root=tmp_path)

    updated = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert updated["status"] == "committed"
    assert report.committed == (manifest["job_id"],)


@pytest.mark.asyncio
async def test_sweep_stops_at_max_retries_and_alerts_exactly_once(tmp_path: Path, monkeypatch) -> None:
    from app.memory.t2.job_sweep import sweep_t2_jobs

    manifest, manifest_path = await _make_held_job(tmp_path=tmp_path, monkeypatch=monkeypatch)
    agent_id = manifest["agent_id"]
    manifest["retry_count"] = 3
    _write_manifest(manifest_path, manifest)
    alerts = _capture_audit_alerts(monkeypatch)

    first = await sweep_t2_jobs(agent_id=agent_id, data_root=tmp_path, max_retries=3)

    updated = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert updated["status"] == "held"
    assert int(updated["retry_count"]) == 3
    assert updated["retry_exhausted_alerted_at"]
    assert first.retried == ()
    assert first.exhausted == (manifest["job_id"],)
    assert first.alerted == (manifest["job_id"],)
    assert len(alerts) == 1
    assert alerts[0]["action"] == "t2_job_retry_exhausted"
    assert alerts[0]["payload"]["job_id"] == manifest["job_id"]

    second = await sweep_t2_jobs(agent_id=agent_id, data_root=tmp_path, max_retries=3)

    assert second.exhausted == (manifest["job_id"],)
    assert second.alerted == ()
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_sweep_repairs_missing_tenant_authority_and_replays_exhausted_job(tmp_path: Path, monkeypatch) -> None:
    """Retries spent without tenant authority are platform debt, not a permanent memory loss verdict."""

    from app.memory.t2.job_sweep import sweep_t2_jobs

    manifest, manifest_path = await _make_held_job(tmp_path=tmp_path, monkeypatch=monkeypatch)
    agent_id = manifest["agent_id"]
    tenant_id = uuid4()
    manifest.update(
        {
            "tenant_id": None,
            "retry_count": 3,
            "retry_exhausted_alerted_at": datetime.now(UTC).isoformat(),
            "issues": ["no summary model config for T0->T2 package build"],
        }
    )
    _write_manifest(manifest_path, manifest)
    phases = _patch_working_llm(monkeypatch)

    report = await sweep_t2_jobs(
        agent_id=agent_id,
        tenant_id=tenant_id,
        data_root=tmp_path,
        max_retries=3,
    )

    updated = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert updated["status"] == "committed"
    assert updated["tenant_id"] == str(tenant_id)
    assert updated["tenant_authority_repaired_at"]
    assert updated["authority_repaired_from_retry_count"] == 3
    assert updated["retry_count"] == 1
    assert "retry_exhausted_alerted_at" not in updated
    assert phases == ["summary", "labels", "review"]
    assert report.retried == (manifest["job_id"],)
    assert report.committed == (manifest["job_id"],)


@pytest.mark.asyncio
async def test_sweep_replaces_mismatched_tenant_with_agent_authority(tmp_path: Path, monkeypatch) -> None:
    """A well-formed but wrong tenant UUID cannot outrank the owning Agent record."""

    from app.memory.t2.job_sweep import sweep_t2_jobs

    manifest, manifest_path = await _make_held_job(tmp_path=tmp_path, monkeypatch=monkeypatch)
    agent_id = manifest["agent_id"]
    wrong_tenant_id = uuid4()
    authoritative_tenant_id = uuid4()
    manifest.update(
        {
            "tenant_id": str(wrong_tenant_id),
            "retry_count": 2,
            "retry_exhausted_alerted_at": datetime.now(UTC).isoformat(),
            "issues": ["opaque prior infrastructure diagnostic"],
        }
    )
    _write_manifest(manifest_path, manifest)
    _patch_working_llm(monkeypatch)

    report = await sweep_t2_jobs(
        agent_id=agent_id,
        tenant_id=authoritative_tenant_id,
        data_root=tmp_path,
    )

    updated = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert updated["status"] == "committed"
    assert updated["tenant_id"] == str(authoritative_tenant_id)
    assert updated["tenant_authority_previous_value"] == str(wrong_tenant_id)
    assert updated["authority_repaired_from_retry_count"] == 2
    assert updated["retry_count"] == 1
    assert "retry_exhausted_alerted_at" not in updated
    assert report.committed == (manifest["job_id"],)


@pytest.mark.asyncio
async def test_sweep_bounds_automatic_replay_and_reports_deferred_jobs(tmp_path: Path, monkeypatch) -> None:
    from app.memory.t2 import job_sweep

    agent_id = uuid4()
    tenant_id = uuid4()
    for suffix in ("a", "b"):
        manifest = _stale_manifest(agent_id=agent_id, status="held")
        manifest["job_id"] = f"t2job-bounded-{suffix}"
        manifest["tenant_id"] = str(tenant_id)
        manifest_path = _jobs_dir(tmp_path, agent_id) / manifest["job_id"] / "job_manifest.json"
        _write_manifest(manifest_path, manifest)

    retried: list[str] = []

    async def fake_retry(manifest_path, manifest, **_kwargs):
        retried.append(str(manifest["job_id"]))
        return "committed"

    monkeypatch.setattr(job_sweep, "_retry_job", fake_retry)

    report = await job_sweep.sweep_t2_jobs(
        agent_id=agent_id,
        tenant_id=tenant_id,
        data_root=tmp_path,
        max_jobs_per_sweep=1,
    )

    assert retried == ["t2job-bounded-a"]
    assert report.retried == ("t2job-bounded-a",)
    assert report.committed == ("t2job-bounded-a",)
    assert report.deferred == ("t2job-bounded-b",)
    control = json.loads(
        (tmp_path / str(agent_id) / "memory" / "control" / "t2_job_sweep.json").read_text(encoding="utf-8")
    )
    assert control["deferred"] == ["t2job-bounded-b"]


@pytest.mark.asyncio
async def test_sweep_replays_persisted_session_lineage(tmp_path: Path, monkeypatch) -> None:
    from app.memory.t2 import job_sweep

    agent_id = uuid4()
    tenant_id = uuid4()
    manifest = _stale_manifest(agent_id=agent_id, status="held")
    manifest["tenant_id"] = str(tenant_id)
    manifest["session_lineage"] = {
        "branch_mode": "rewind",
        "source_session_id": "source-session-1",
        "anchor_event_id": "event-7",
    }
    manifest_path = _jobs_dir(tmp_path, agent_id) / manifest["job_id"] / "job_manifest.json"
    _write_manifest(manifest_path, manifest)
    calls: list[dict] = []

    async def fake_run(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status="committed")

    monkeypatch.setattr("app.memory.t2.segment_package.run_t2_segment_package_job", fake_run)

    report = await job_sweep.sweep_t2_jobs(
        agent_id=agent_id,
        tenant_id=tenant_id,
        data_root=tmp_path,
    )

    assert report.committed == (manifest["job_id"],)
    assert calls[0]["session_lineage"] == manifest["session_lineage"]


@pytest.mark.asyncio
async def test_sweep_skips_committed_jobs(tmp_path: Path, monkeypatch) -> None:
    from app.memory.t2.job_sweep import sweep_t2_jobs
    from app.memory.t2.segment_package import run_t2_segment_package_job

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    event = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="user_message",
        role="user",
        content="committed job 不应被 sweep 触碰。",
        source="web",
        data_root=tmp_path,
    )
    phases = _patch_working_llm(monkeypatch)
    result = await run_t2_segment_package_job(
        data_root=tmp_path,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        t0_segment_id=event.segment_id,
    )
    assert result.status == "committed"
    phases.clear()

    report = await sweep_t2_jobs(agent_id=agent_id, data_root=tmp_path)

    assert report.scanned == 1
    assert report.retried == ()
    assert report.committed == ()
    assert phases == []
    manifest = json.loads((result.staging_dir / "job_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "committed"
    assert "retry_count" not in manifest


@pytest.mark.asyncio
async def test_sweep_handles_missing_jobs_dir(tmp_path: Path) -> None:
    from app.memory.t2.job_sweep import sweep_t2_jobs

    report = await sweep_t2_jobs(agent_id=uuid4(), data_root=tmp_path)

    assert report.scanned == 0
    assert report.recovered_stale == ()
    assert report.retried == ()


@pytest.mark.asyncio
async def test_sweep_writes_control_report(tmp_path: Path, monkeypatch) -> None:
    from app.memory.t2.job_sweep import sweep_t2_jobs

    manifest, _ = await _make_held_job(tmp_path=tmp_path, monkeypatch=monkeypatch)
    agent_id = manifest["agent_id"]

    await sweep_t2_jobs(agent_id=agent_id, data_root=tmp_path)

    report_path = tmp_path / agent_id / "memory" / "control" / "t2_job_sweep.json"
    assert report_path.exists()
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["schema"] == "t2_job_sweep.v1"
    assert payload["agent_id"] == agent_id
    assert payload["scanned"] == 1
    assert payload["still_held"] == [manifest["job_id"]]
    assert payload["generated_at"]


# --- production wiring ---


@pytest.mark.asyncio
async def test_heartbeat_maintenance_runs_t2_job_sweep(tmp_path: Path, monkeypatch) -> None:
    """Heartbeat wrapper must call the real sweep against AGENT_DATA_DIR."""
    from app.services import heartbeat

    agent_id = uuid4()
    manifest = _stale_manifest(agent_id=agent_id, status="running")
    manifest_path = _jobs_dir(tmp_path, agent_id) / manifest["job_id"] / "job_manifest.json"
    _write_manifest(manifest_path, manifest)

    monkeypatch.setattr("app.config.get_settings", lambda: type("S", (), {"AGENT_DATA_DIR": str(tmp_path)})())

    report = await heartbeat._run_t2_job_sweep(agent_id)

    assert report is not None
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["status"] == "held"


def test_execute_heartbeat_wires_t2_job_sweep() -> None:
    import inspect

    from app.services import heartbeat

    source = inspect.getsource(heartbeat._execute_heartbeat)
    assert "_run_t2_job_sweep(" in source


def test_startup_wires_stale_t2_job_sweep() -> None:
    main_source = Path("app/main.py").read_text(encoding="utf-8")
    assert "sweep_all_agents_stale_t2_jobs" in main_source
