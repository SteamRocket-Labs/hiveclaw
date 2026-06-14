from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import eval_ci as eval_ci_api


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(eval_ci_api.router)
    return TestClient(app)


def test_eval_ci_behavior_endpoint_is_disabled_without_token(monkeypatch) -> None:
    monkeypatch.delenv("HIVE_EVAL_CI_TOKEN", raising=False)

    response = _client().post("/eval-ci/behavior", headers={"Authorization": "Bearer token"})

    assert response.status_code == 404
    assert response.json()["detail"] == "eval ci endpoint disabled"


def test_eval_ci_behavior_endpoint_rejects_wrong_token(monkeypatch) -> None:
    monkeypatch.setenv("HIVE_EVAL_CI_TOKEN", "expected-token")

    response = _client().post("/eval-ci/behavior", headers={"Authorization": "Bearer wrong-token"})

    assert response.status_code == 403
    assert response.json()["detail"] == "invalid eval ci token"


def test_eval_ci_behavior_endpoint_runs_production_eval(monkeypatch) -> None:
    monkeypatch.setenv("HIVE_EVAL_CI_TOKEN", "expected-token")
    captured: dict[str, object] = {}

    async def fake_run(*, scenarios=None):
        captured["scenarios"] = scenarios
        return {
            "kind": "behavior_eval",
            "transport": "hive_live",
            "runtime": {"model": "claude-sonnet-4-5"},
            "benchmark_complete": True,
            "fallback_used": False,
            "scenarios": {"coding": {"ready": True, "score": 100}},
        }

    monkeypatch.setattr(eval_ci_api, "run_production_behavior_eval_for_ci", fake_run)

    response = _client().post(
        "/eval-ci/behavior",
        headers={"Authorization": "Bearer expected-token"},
        json={"scenarios": ["coding"]},
    )

    assert response.status_code == 200
    assert response.json()["transport"] == "hive_live"
    assert response.json()["runtime"]["model"] == "claude-sonnet-4-5"
    assert captured["scenarios"] == ("coding",)


def test_eval_ci_router_is_registered_with_api_prefix() -> None:
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "app" / "main.py").read_text(encoding="utf-8")

    assert "from app.api.eval_ci import router as eval_ci_router" in source
    assert "eval_ci_router" in source
