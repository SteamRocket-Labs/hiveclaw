from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_maybe_run_skill_distillation_respects_feature_flag(monkeypatch, tmp_path: Path) -> None:
    from app.services.heartbeat import _maybe_run_skill_distillation

    called: list[dict] = []

    async def fake_run_skill_distillation_cycle(**kwargs):
        called.append(kwargs)
        return {"status": "promoted"}

    monkeypatch.setattr(
        "app.services.skill_distiller.run_skill_distillation_cycle",
        fake_run_skill_distillation_cycle,
    )

    await _maybe_run_skill_distillation(
        agent_id=uuid4(),
        workspace=tmp_path,
        tenant_id=None,
        runtime_config=SimpleNamespace(skill_candidate_loop_enabled=False),
        model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="test-key", base_url=None),
        current_session_id="session-1",
    )
    assert called == []

    await _maybe_run_skill_distillation(
        agent_id=uuid4(),
        workspace=tmp_path,
        tenant_id=None,
        runtime_config=SimpleNamespace(skill_candidate_loop_enabled=True),
        model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="test-key", base_url=None),
        current_session_id="session-2",
    )

    assert len(called) == 1
    assert called[0]["current_session_id"] == "session-2"
