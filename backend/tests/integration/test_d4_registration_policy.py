"""Closed registration does not disclose existing identities."""

from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.api import auth
from app.database import get_db
from app.models.user import User


@pytest.mark.parametrize("collision", ["email", "username", "feishu_shadow"])
async def test_closed_registration_refuses_existing_and_unknown_accounts_identically(
    owner_sessionmaker, monkeypatch, collision,
):
    suffix = uuid4().hex
    existing = User(username=f"occupied-{suffix}", email=f"{suffix}@example.com",
        display_name="Existing member", password_hash="local-only",
        feishu_open_id=f"ou-{suffix}" if collision == "feishu_shadow" else None,
        must_change_password=collision == "feishu_shadow")
    async with owner_sessionmaker() as db:
        db.add(existing)
        await db.commit()
        initial_count = await db.scalar(select(func.count()).select_from(User))

    monkeypatch.setattr(auth, "get_settings", lambda: SimpleNamespace(
        PUBLIC_REGISTRATION_ENABLED=False, INSTALL_TOKEN="local-only"))

    async def database():
        async with owner_sessionmaker() as db:
            yield db

    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    app.dependency_overrides[get_db] = database
    unknown = {"username": f"new-{suffix}", "email": f"new-{suffix}@example.com", "password": "LocalPassword123!"}
    occupied = {**unknown, **({"username": existing.username} if collision == "username" else {"email": existing.email})}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        refused_unknown = await client.post("/api/auth/register", json=unknown)
        refused_existing = await client.post("/api/auth/register", json=occupied)
    assert refused_unknown.status_code == refused_existing.status_code == 403
    assert refused_unknown.json() == refused_existing.json() == {
        "detail": "Public registration is closed. Sign in with an existing account or SSO."
    }
    async with owner_sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(User)) == initial_count
