from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import eval_ci_service


@pytest.mark.asyncio
async def test_production_behavior_eval_for_ci_uses_tenant_runtime(monkeypatch) -> None:
    tenant_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    model = SimpleNamespace(id=uuid4(), provider="anthropic", model="claude-sonnet-4-5")
    captured: dict[str, object] = {}

    monkeypatch.setenv("HIVE_EVAL_TENANT_ID", str(tenant_id))

    async def fake_resolve_production_eval_runtime(**kwargs):
        captured["resolve_kwargs"] = kwargs
        return SimpleNamespace(
            tenant_id=tenant_id,
            agent_id=agent_id,
            user_id=user_id,
            model=model,
            fallback_model=None,
            agent_name="Eval Agent",
            role_description="Evaluate behavior",
            model_source="agent.primary_model_id",
        )

    def fake_build_invoke_agent_runner(**kwargs):
        captured["runner_kwargs"] = kwargs
        return object()

    async def fake_run_hive_behavior_eval(**kwargs):
        captured["run_kwargs"] = kwargs
        return {
            "kind": "behavior_eval",
            "transport": "hive_live",
            "runtime": {"executable": "invoke_agent"},
            "benchmark_complete": True,
            "fallback_used": False,
            "scenarios": {"coding": {"ready": True, "score": 100}},
        }

    monkeypatch.setattr(eval_ci_service, "resolve_production_eval_runtime", fake_resolve_production_eval_runtime)
    monkeypatch.setattr(eval_ci_service, "build_invoke_agent_runner", fake_build_invoke_agent_runner)
    monkeypatch.setattr(eval_ci_service, "run_hive_behavior_eval", fake_run_hive_behavior_eval)

    report = await eval_ci_service.run_production_behavior_eval_for_ci(scenarios=("coding",))

    assert captured["resolve_kwargs"]["expected_tenant_id"] == str(tenant_id)
    assert captured["runner_kwargs"]["model"] is model
    assert captured["runner_kwargs"]["agent_id"] == agent_id
    assert captured["run_kwargs"]["scenarios"] == ("coding",)
    assert report["runtime"]["model"] == "claude-sonnet-4-5"
    assert report["runtime"]["tenant_id"] == str(tenant_id)
    assert report["runtime"]["agent_id"] == str(agent_id)
    assert report["runtime"]["user_id"] == str(user_id)


@pytest.mark.asyncio
async def test_production_behavior_eval_for_ci_requires_eval_tenant(monkeypatch) -> None:
    monkeypatch.delenv("HIVE_EVAL_TENANT_ID", raising=False)

    with pytest.raises(RuntimeError, match="HIVE_EVAL_TENANT_ID is required"):
        await eval_ci_service.run_production_behavior_eval_for_ci()
