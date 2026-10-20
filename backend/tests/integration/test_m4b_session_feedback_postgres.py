"""A session rating cannot silently become an active long-term memory."""

from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text

from app.api import chat_sessions
from app.config import get_settings
from app.core.security import create_access_token
from app.database import get_db, tenant_scoped_session
from app.models.audit import AuditLog
from app.models.session_feedback import SessionFeedbackEvent
from app.models.user import User
from tests.integration import conftest as persistence
from tests.services.test_session_input_control_v2 import _seed_session

app_user_engine = persistence.app_user_engine
app_user_sessionmaker = persistence.app_user_sessionmaker
migrated_pg_url = persistence.migrated_pg_url
owner_engine = persistence.owner_engine
owner_sessionmaker = persistence.owner_sessionmaker
pg_container = persistence.pg_container


@pytest.mark.parametrize("label", ["useful", "misleading"])
@pytest.mark.parametrize("reason", ["", "The answer used the right source; keep this evaluation with this work."])
async def test_real_session_rating_records_evaluation_without_creating_durable_memory(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, label, reason
):
    from app.memory.write_gate import MemoryWriteDecision

    tenant, user, agent, session, _run = await _seed_session(owner_sessionmaker)
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    memory_reviews = []

    async def reference_review(content, *, category, **_kwargs):
        memory_reviews.append(content)
        return MemoryWriteDecision(
            original_content=content,
            content=content,
            category=category,
            sensitivity="PL1_public",
            metadata={"threat_gate_method": "local_reference_reviewer"},
        )

    monkeypatch.setattr("app.memory.explicit_overlay.prepare_memory_write_with_llm", reference_review)

    async def scoped_db():
        async with tenant_scoped_session(tenant, session_factory=app_user_sessionmaker) as db:
            yield db

    app = FastAPI()
    app.include_router(chat_sessions.router)
    app.dependency_overrides[get_db] = scoped_db
    headers = {"Authorization": "Bearer " + create_access_token(str(user), "member", str(tenant))}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as api:
        result = await api.post(
            f"/agents/{agent}/sessions/{session}/feedback", headers=headers, json={"label": label, "reason": reason}
        )
    assert result.status_code == 200, result.text
    assert memory_reviews == [], "Rating a session started a durable-memory generation"
    assert not (tmp_path / str(agent) / "memory/explicit").exists()
    calibration = result.json()["calibration_result"]
    assert calibration["memory_status"] == calibration["t3_status"] == "not_requested"
    assert not calibration.get("entry_id")
    async with tenant_scoped_session(tenant, session_factory=app_user_sessionmaker) as db:
        rating = await db.scalar(select(SessionFeedbackEvent))
        assert (rating.user_id, rating.session_id, rating.agent_id, rating.label, rating.reason) == (
            user, session, agent, label, reason
        )
        audit = await db.scalar(select(AuditLog).where(AuditLog.action == "session_feedback.recorded"))
        assert audit.user_id == user and audit.details["label"] == label
        assert await db.scalar(text("SELECT count(*) FROM runtime_tasks")) == 0
        assert not await db.scalar(text("SELECT rolbypassrls OR rolsuper FROM pg_roles WHERE rolname=current_user"))


async def test_another_members_private_session_cannot_receive_a_rating(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path
):
    tenant, _user, agent, session, _run = await _seed_session(owner_sessionmaker)
    other_user = uuid4()
    async with owner_sessionmaker() as db:
        db.add(User(id=other_user, tenant_id=tenant, username=other_user.hex, email=f"{other_user}@fixture.test",
            display_name="Other member", password_hash="synthetic", role="member"))
        await db.commit()
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))

    async def scoped_db():
        async with tenant_scoped_session(tenant, session_factory=app_user_sessionmaker) as db:
            yield db

    app = FastAPI()
    app.include_router(chat_sessions.router)
    app.dependency_overrides[get_db] = scoped_db
    headers = {"Authorization": "Bearer " + create_access_token(str(other_user), "member", str(tenant))}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as api:
        result = await api.post(
            f"/agents/{agent}/sessions/{session}/feedback", headers=headers, json={"label": "useful"}
        )
    assert result.status_code in {403, 404}, result.text
    assert not (tmp_path / str(agent) / "memory").exists()
    async with tenant_scoped_session(tenant, session_factory=app_user_sessionmaker) as db:
        assert await db.scalar(text("SELECT count(*) FROM session_feedback_events")) == 0
