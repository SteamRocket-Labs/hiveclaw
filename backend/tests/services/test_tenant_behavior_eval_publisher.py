from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


def _passing_report() -> dict:
    return {
        "kind": "behavior_eval",
        "transport": "hive_live",
        "benchmark_complete": True,
        "fallback_used": False,
        "scenarios": {"coding": {"ready": True, "score": 100}},
    }


@pytest.mark.asyncio
async def test_run_and_store_tenant_behavior_eval_writes_requested_tenant(monkeypatch, tmp_path):
    from app.services import tenant_behavior_eval_publisher as publisher

    tenant_id = uuid4()
    fallback_agent_id = uuid4()
    stored: dict = {}

    async def fake_resolve_runtime(**kwargs):
        assert kwargs["tenant_id"] == tenant_id
        assert kwargs["fallback_agent_id"] == fallback_agent_id
        return SimpleNamespace(
            tenant_id=tenant_id,
            agent_id=fallback_agent_id,
            user_id=uuid4(),
            model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None),
            fallback_model=None,
            agent_name="Eval Agent",
            role_description="Run behavior eval.",
            model_source="fallback_agent.primary_model_id",
        )

    async def fake_run_hive_behavior_eval(**kwargs):
        assert kwargs["scenarios"] == ("coding",)
        return _passing_report()

    async def fake_store(*, tenant_id: object, report: dict):
        stored["tenant_id"] = tenant_id
        stored["report"] = report
        return {"stored_at": "2026-06-15T00:00:00+00:00", "report": report}

    monkeypatch.setattr(publisher, "resolve_tenant_behavior_eval_runtime", fake_resolve_runtime)
    monkeypatch.setattr(publisher, "run_hive_behavior_eval", fake_run_hive_behavior_eval)
    monkeypatch.setattr(publisher, "store_latest_behavior_eval_report_for_tenant", fake_store)

    report = await publisher.run_and_store_tenant_behavior_eval(
        tenant_id=tenant_id,
        fallback_agent_id=fallback_agent_id,
        scenarios=("coding",),
        output_root=tmp_path,
    )

    assert report["scenarios"] == _passing_report()["scenarios"]
    assert report["runtime"]["tenant_id"] == str(tenant_id)
    assert report["runtime"]["agent_id"] == str(fallback_agent_id)
    assert report["runtime"]["publisher"] == "tenant_behavior_eval_publisher"
    assert stored == {"tenant_id": tenant_id, "report": report}


@pytest.mark.asyncio
async def test_ensure_skill_distiller_behavior_report_injects_passing_report(monkeypatch, tmp_path):
    from app.services import tenant_behavior_eval_publisher as publisher

    tenant_id = uuid4()
    agent_id = uuid4()
    runtime_config = SimpleNamespace(skill_candidate_loop_enabled=True, skill_distiller_behavior_report=None)
    report = _passing_report()

    monkeypatch.setattr(publisher, "_auto_publish_enabled", lambda: True)

    async def fake_load_latest(*, tenant_id: object):
        return None

    async def fake_run_and_store(**kwargs):
        assert kwargs["tenant_id"] == tenant_id
        assert kwargs["fallback_agent_id"] == agent_id
        return report

    monkeypatch.setattr(publisher, "load_latest_behavior_eval_value_for_tenant", fake_load_latest)
    monkeypatch.setattr(publisher, "run_and_store_tenant_behavior_eval", fake_run_and_store)

    ensured = await publisher.ensure_skill_distiller_behavior_report(
        agent_id=agent_id,
        tenant_id=tenant_id,
        runtime_config=runtime_config,
        output_root=tmp_path,
    )

    assert ensured == report
    assert runtime_config.skill_distiller_behavior_report == report


@pytest.mark.asyncio
async def test_ensure_skill_distiller_behavior_report_respects_recent_failed_report(monkeypatch, tmp_path):
    from app.services import tenant_behavior_eval_publisher as publisher

    runtime_config = SimpleNamespace(skill_candidate_loop_enabled=True, skill_distiller_behavior_report=None)
    failed_report = {
        "kind": "behavior_eval",
        "transport": "hive_live",
        "benchmark_complete": True,
        "fallback_used": False,
        "scenarios": {"coding": {"ready": False, "score": 0}},
    }

    monkeypatch.setattr(publisher, "_auto_publish_enabled", lambda: True)
    monkeypatch.setattr(publisher, "_report_max_age_hours", lambda: 24)

    async def fake_load_latest(*, tenant_id):
        return {"stored_at": "2026-06-15T00:00:00+00:00", "report": failed_report}

    async def fail_run_and_store(**_kwargs):
        raise AssertionError("fresh failed reports should throttle re-runs")

    monkeypatch.setattr(publisher, "_now_utc", lambda: publisher._parse_stored_at("2026-06-15T01:00:00+00:00"))
    monkeypatch.setattr(publisher, "load_latest_behavior_eval_value_for_tenant", fake_load_latest)
    monkeypatch.setattr(publisher, "run_and_store_tenant_behavior_eval", fail_run_and_store)

    ensured = await publisher.ensure_skill_distiller_behavior_report(
        agent_id=uuid4(),
        tenant_id=uuid4(),
        runtime_config=runtime_config,
        output_root=tmp_path,
    )

    assert ensured is None
    assert runtime_config.skill_distiller_behavior_report is None
