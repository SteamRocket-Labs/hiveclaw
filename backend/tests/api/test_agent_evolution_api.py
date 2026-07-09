from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient

import app.api.agents as agents_mod
from app.api.agents import router
from app.core.security import get_current_user
from app.database import get_db


class _FakeDB:
    async def execute(self, _stmt):
        raise AssertionError("Unexpected execute() call")


def _build_client():
    app = FastAPI()
    app.include_router(router)
    fake_db = _FakeDB()
    current_user = SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4(), is_active=True)

    async def override_user():
        return current_user

    async def override_db():
        yield fake_db

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    return TestClient(app, raise_server_exceptions=False), fake_db, current_user


def _seed_workspace(ws: Path) -> None:
    (ws / "skills").mkdir(parents=True, exist_ok=True)
    (ws / "evolution").mkdir(parents=True, exist_ok=True)
    (ws / "memory" / "knowledge").mkdir(parents=True, exist_ok=True)
    usage = {
        "weekly-report": {
            "created_by": "agent",
            "created_at": "2026-01-01T08:00:00+00:00",
            "last_used_at": "2026-05-20T09:00:00+00:00",
            "use_count": 12,
            "state": "active",
            "pinned": False,
            "archived_at": None,
        },
        "old-scraper": {
            "created_by": "agent",
            "created_at": "2025-10-01T08:00:00+00:00",
            "last_used_at": "2025-11-01T09:00:00+00:00",
            "use_count": 2,
            "state": "archived",
            "pinned": False,
            "archived_at": "2026-02-01T00:00:00+00:00",
        },
    }
    (ws / "evolution" / "skill_usage.json").write_text(json.dumps(usage), encoding="utf-8")
    (ws / "evolution" / "skill_registry.json").write_text(
        json.dumps(
            {
                "schema": "skill_registry.v1",
                "skills": {
                    "weekly-report": {
                        "skill_name": "weekly-report",
                        "skill_origin": "user_skill_creator",
                        "evolvable": True,
                        "state": "active",
                        "target_path": "skills/weekly-report/SKILL.md",
                    },
                    "old-scraper": {
                        "skill_name": "old-scraper",
                        "skill_origin": "t3_auto_created",
                        "evolvable": True,
                        "state": "archived",
                        "target_path": "skills/old-scraper/SKILL.md",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    (ws / "memory" / "knowledge" / "weekly-report.md").write_text(
        "---\ntitle: Weekly Report\nstatus: active\n---\n\n## Current Claim\nWeekly report package pattern.\n",
        encoding="utf-8",
    )
    (ws / "evolution" / "skill_review.md").write_text(
        "# Skill Review\n\n- 2026-05-20T09:05:00+00:00 [promote] weekly-report: 3 successes\n",
        encoding="utf-8",
    )
    (ws / "evolution" / "evolution_ledger.jsonl").write_text(
        json.dumps(
            {
                "schema": "evolution_promotion_decision.v1",
                "event": "promotion_decision",
                "candidate_id": "abc123",
                "target_id": "weekly-report",
                "decision": "promote",
                "reason": "beat baseline",
                "created_at": "2026-05-21T08:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_get_agent_evolution_returns_structured_view(monkeypatch, tmp_path):
    expected_agent_id = uuid4()
    client, fake_db, current_user = _build_client()
    ws = tmp_path / str(expected_agent_id)
    _seed_workspace(ws)

    async def fake_check_agent_access(db_session, user, target_agent_id):
        assert db_session is fake_db
        assert user is current_user
        assert target_agent_id == expected_agent_id
        return SimpleNamespace(id=expected_agent_id), "use"

    monkeypatch.setattr(agents_mod, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(
        agents_mod, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path))
    )

    response = client.get(f"/agents/{expected_agent_id}/evolution")

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema"] == "agent_evolution_view.v2"
    assert payload["path_contract"]["t3_knowledge_pages"] == "memory/knowledge/<slug>.md"
    assert "t3_capabilities" not in payload["path_contract"]
    assert payload["skill_ecosystem"]["summary"]["active"] == 1
    assert payload["skill_ecosystem"]["summary"]["archived"] == 1
    assert payload["skill_ecosystem"]["summary"]["evolvable"] == 2
    assert payload["skill_ecosystem"]["skills"][0]["skill_name"] == "weekly-report"
    assert payload["skill_ecosystem"]["skills"][0]["skill_origin"] == "user_skill_creator"
    assert payload["memory_learning"]["t3_targets"]["knowledge_pages"]["line_count"] > 0


@pytest.mark.asyncio
async def test_get_agent_evolution_empty_when_no_workspace(monkeypatch, tmp_path):
    expected_agent_id = uuid4()
    client, fake_db, current_user = _build_client()

    async def fake_check_agent_access(db_session, user, target_agent_id):
        return SimpleNamespace(id=expected_agent_id), "use"

    monkeypatch.setattr(agents_mod, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(
        agents_mod, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path))
    )

    response = client.get(f"/agents/{expected_agent_id}/evolution")

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema"] == "agent_evolution_view.v2"
    assert payload["skill_ecosystem"]["summary"]["total"] == 0
    assert payload["skill_ecosystem"]["skills"] == []
    assert payload["skill_tuning"]["candidates"] == []
    assert payload["legacy_audit"]["detected_legacy_files"] == []


@pytest.mark.asyncio
async def test_get_agent_evolution_denies_unauthorized(monkeypatch, tmp_path):
    expected_agent_id = uuid4()
    client, _fake_db, _current_user = _build_client()

    async def fake_check_agent_access(db_session, user, target_agent_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this agent")

    monkeypatch.setattr(agents_mod, "check_agent_access", fake_check_agent_access)
    # Settings must never be read once access is denied.
    monkeypatch.setattr(
        agents_mod,
        "get_settings",
        lambda: (_ for _ in ()).throw(AssertionError("settings read after access denied")),
    )

    response = client.get(f"/agents/{expected_agent_id}/evolution")

    assert response.status_code == 403
