"""P1-W3-10 — dream consolidation audit + metrics surface.

Dream's autonomous LLM call used to vanish from any audit trail because
it bypasses invoke_agent. Now every consolidation run:
  - bumps a `record_autonomous_llm_call(source='dream', outcome=...)` counter
  - writes a `dream.llm_consolidation` audit log entry

Both signals show up regardless of whether the LLM call succeeded,
failed, or returned an unparseable decision — so operators can chart
success rate alongside volume.
"""

from __future__ import annotations

import json
import re
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.memory import metrics
from app.services import auto_dream


@pytest.fixture(autouse=True)
def reset_metrics():
    metrics.reset_all()
    yield
    metrics.reset_all()


def _make_model_config():
    return {"provider": "openai", "model": "gpt-4o-mini"}


@pytest.fixture
def stub_model_resolver(monkeypatch):
    monkeypatch.setattr(
        "app.services.memory_service._get_summary_model_config",
        AsyncMock(return_value=_make_model_config()),
    )


@pytest.fixture
def capture_audit_writes(monkeypatch):
    captured: list[dict] = []

    async def _fake_write(action, details=None, agent_id=None, user_id=None):
        captured.append(
            {
                "action": action,
                "details": details,
                "agent_id": agent_id,
                "user_id": user_id,
            }
        )

    monkeypatch.setattr("app.services.audit_logger.write_audit_log", _fake_write)
    return captured


# ── Successful path ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_successful_call_records_success_metric_and_audit(
    monkeypatch, tmp_path, stub_model_resolver, capture_audit_writes
):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path), raising=False)

    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    (tmp_path / str(agent_id)).mkdir()

    class _StubClient:
        async def stream(self, **_kw):
            manifest = re.findall(
                r'<item path="([^"]+)" sha256="([^"]+)"',
                _kw["messages"][1].content,
            )
            return SimpleNamespace(
                content=json.dumps(
                    {
                        "reasoning": "ok",
                        "coverage_receipt": [
                            {"path": path, "sha256": digest, "status": "reviewed"}
                            for path, digest in manifest
                        ],
                        "soul_candidate": None,
                        "t3_patch_concerns": [],
                    }
                )
            )

        async def close(self):
            pass

    monkeypatch.setattr(
        "app.services.llm_client.create_llm_client",
        lambda **_kw: _StubClient(),
    )

    decision = await auto_dream._dream_llm_consolidate(agent_id, tenant_id, {"feedback.md": "x"}, "Agent")
    assert decision is not None

    snap = metrics.snapshot()
    assert snap["autonomous_llm_calls_total"].get("dream:success") == 1
    assert snap["autonomous_llm_calls_total"].get("dream:failure", 0) == 0

    assert len(capture_audit_writes) == 1
    log = capture_audit_writes[0]
    assert log["action"] == "dream.llm_consolidation"
    assert log["agent_id"] == agent_id
    assert log["details"]["outcome"] == "success"
    assert log["details"]["tenant_id"] == str(tenant_id)


# ── Failure paths ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_exception_records_failure_metric_and_audit(
    monkeypatch, tmp_path, stub_model_resolver, capture_audit_writes
):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path), raising=False)
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    (tmp_path / str(agent_id)).mkdir()

    class _BoomClient:
        async def stream(self, **_kw):
            raise ConnectionError("LLM down")

        async def close(self):
            pass

    monkeypatch.setattr(
        "app.services.llm_client.create_llm_client",
        lambda **_kw: _BoomClient(),
    )

    decision = await auto_dream._dream_llm_consolidate(agent_id, tenant_id, {"feedback.md": "x"}, "Agent")
    assert decision is None

    snap = metrics.snapshot()
    assert snap["autonomous_llm_calls_total"].get("dream:failure") == 1

    assert len(capture_audit_writes) == 1
    assert capture_audit_writes[0]["details"]["outcome"] == "failure"
    assert capture_audit_writes[0]["details"]["reason"] == "ConnectionError"


@pytest.mark.asyncio
async def test_unparseable_decision_records_failure_with_reason(
    monkeypatch, tmp_path, stub_model_resolver, capture_audit_writes
):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path), raising=False)
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    (tmp_path / str(agent_id)).mkdir()

    class _GarbageClient:
        async def stream(self, **_kw):
            return SimpleNamespace(content="this isn't JSON at all")

        async def close(self):
            pass

    monkeypatch.setattr(
        "app.services.llm_client.create_llm_client",
        lambda **_kw: _GarbageClient(),
    )

    decision = await auto_dream._dream_llm_consolidate(agent_id, tenant_id, {"feedback.md": "x"}, "Agent")
    assert decision is None

    snap = metrics.snapshot()
    assert snap["autonomous_llm_calls_total"].get("dream:failure") == 1

    assert len(capture_audit_writes) == 1
    assert capture_audit_writes[0]["details"]["reason"] == "unparseable_decision"


# ── Skipped paths leave the counter alone ────────────────────


@pytest.mark.asyncio
async def test_no_tenant_id_does_not_emit_metric(monkeypatch, capture_audit_writes):
    """Early return on missing tenant must not pollute the counter."""
    decision = await auto_dream._dream_llm_consolidate(uuid.uuid4(), None, {"f.md": "x"}, "Agent")
    assert decision is None
    snap = metrics.snapshot()
    assert snap["autonomous_llm_calls_total"] == {}
    assert capture_audit_writes == []


# ── Snapshot shape ───────────────────────────────────────────


def test_snapshot_includes_autonomous_llm_calls_key_when_empty():
    """Even with zero events, the dashboard surface must list the key
    so ops UIs don't render NaN cards."""
    snap = metrics.snapshot()
    assert "autonomous_llm_calls_total" in snap
    assert snap["autonomous_llm_calls_total"] == {}
