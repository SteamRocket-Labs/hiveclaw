"""P1-W3-11 — skill_distiller LLM call surfaces in metrics + audit.

Same shape as the dream audit (P1-W3-10): every LLM draft attempt bumps
the autonomous call counter and writes a `skill_distiller.llm_draft`
audit log entry, regardless of whether the call succeeded, raised, or
returned unparseable JSON.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.memory import metrics
from app.services import skill_distiller


@pytest.fixture(autouse=True)
def reset_metrics():
    metrics.reset_all()
    yield
    metrics.reset_all()


@pytest.fixture
def capture_audit_writes(monkeypatch):
    captured: list[dict] = []

    async def _fake_write(action, details=None, agent_id=None, user_id=None):
        captured.append({
            "action": action,
            "details": details,
            "agent_id": agent_id,
        })

    monkeypatch.setattr("app.services.audit_logger.write_audit_log", _fake_write)
    return captured


def _model_stub():
    return SimpleNamespace(
        provider="openai",
        model="gpt-4o-mini",
        api_key="k",
        base_url=None,
        max_output_tokens=1400,
    )


def _make_workspace(tmp_path: Path) -> Path:
    """Create a workspace directory whose name is a valid UUID — the
    distiller derives agent_id from the path's last segment."""
    agent_id = uuid.uuid4()
    ws = tmp_path / str(agent_id)
    ws.mkdir()
    return ws


# ── Success path ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_successful_draft_records_success_metric_and_audit(
    monkeypatch, tmp_path, capture_audit_writes
):
    payload = {
        "decision": "defer",
        "confidence": 0.5,
        "name": "",
        "description": "",
        "instructions_markdown": "",
        "declared_tools": [],
        "declared_packs": [],
        "reason": "thin evidence",
    }

    class _StubClient:
        async def complete(self, **_kw):
            return SimpleNamespace(content=json.dumps(payload))

        async def close(self):
            pass

    monkeypatch.setattr(skill_distiller, "create_llm_client", lambda **_kw: _StubClient())

    ws = _make_workspace(tmp_path)
    draft = await skill_distiller._draft_skill_with_llm(
        model=_model_stub(),
        workflow_signature="x->y",
        evidence=[],
        declared_packs=(),
        workspace=ws,
    )

    assert draft.decision == "defer"

    snap = metrics.snapshot()
    assert snap["autonomous_llm_calls_total"].get("skill_distiller:success") == 1

    assert len(capture_audit_writes) == 1
    log = capture_audit_writes[0]
    assert log["action"] == "skill_distiller.llm_draft"
    assert log["details"]["outcome"] == "success"
    assert log["details"]["decision"] == "defer"
    # Agent ID derived from workspace name.
    assert str(log["agent_id"]) == ws.name


# ── Failure paths ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_exception_records_failure_and_reraises(
    monkeypatch, tmp_path, capture_audit_writes
):
    class _BoomClient:
        async def complete(self, **_kw):
            raise TimeoutError("upstream slow")

        async def close(self):
            pass

    monkeypatch.setattr(skill_distiller, "create_llm_client", lambda **_kw: _BoomClient())

    ws = _make_workspace(tmp_path)
    with pytest.raises(TimeoutError):
        await skill_distiller._draft_skill_with_llm(
            model=_model_stub(),
            workflow_signature="x->y",
            evidence=[],
            declared_packs=(),
            workspace=ws,
        )

    snap = metrics.snapshot()
    assert snap["autonomous_llm_calls_total"].get("skill_distiller:failure") == 1

    assert len(capture_audit_writes) == 1
    log = capture_audit_writes[0]
    assert log["details"]["outcome"] == "failure"
    assert log["details"]["reason"] == "TimeoutError"


@pytest.mark.asyncio
async def test_unparseable_json_records_failure_and_reraises(
    monkeypatch, tmp_path, capture_audit_writes
):
    class _GarbageClient:
        async def complete(self, **_kw):
            return SimpleNamespace(content="not json at all")

        async def close(self):
            pass

    monkeypatch.setattr(skill_distiller, "create_llm_client", lambda **_kw: _GarbageClient())

    ws = _make_workspace(tmp_path)
    with pytest.raises(Exception):
        await skill_distiller._draft_skill_with_llm(
            model=_model_stub(),
            workflow_signature="x->y",
            evidence=[],
            declared_packs=(),
            workspace=ws,
        )

    snap = metrics.snapshot()
    assert snap["autonomous_llm_calls_total"].get("skill_distiller:failure") == 1

    assert len(capture_audit_writes) == 1
    assert capture_audit_writes[0]["details"]["reason"] == "unparseable_json"
