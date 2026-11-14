"""X7b: readable model-facing authors and identity-based plaza statistics."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.api import plaza as plaza_api
from app.core.security import get_current_user
from app.database import get_db, tenant_scoped_session
from app.models.agent import Agent
from app.models.participant import Participant
from app.models.plaza import PlazaComment, PlazaPost
from app.models.tenant import Tenant
from app.models.user import User
from app.services.agent_tool_domains import plaza


@pytest.fixture
async def plaza_seed(owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    async with owner_sessionmaker() as db:
        tenant = Tenant(name="X7b company", slug=f"x7b-{uuid4().hex[:12]}")
        foreign = Tenant(name="Other company", slug=f"x7b-{uuid4().hex[:12]}")
        db.add_all([tenant, foreign])
        await db.flush()
        users = [User(username=f"x7b-{uuid4().hex}", email=f"{uuid4().hex}@example.test",
                      password_hash="not-used", display_name="Member", role="member", tenant_id=tenant.id)
                 for _ in range(2)]
        db.add_all(users)
        await db.flush()
        own, other, hr = agents = [Agent(
            name=name, tenant_id=tenant.id, creator_id=users[index % 2].id,
            agent_class="internal_system" if name == "__system_hr__" else "internal_tenant",
        ) for index, name in enumerate(["My Assistant", "My Assistant", "__system_hr__"])]
        db.add_all(agents)
        await db.flush()
        participant = await db.get(Participant, hr.participant_id)
        participant.display_name = "HR Onboarding Agent"
        now = datetime.now(timezone.utc)

        def post(author, name=None, *, age=0, author_type="agent", tenant_id=None):
            return PlazaPost(
                id=uuid4(), tenant_id=tenant_id or tenant.id, author_id=author.id,
                author_type=author_type, author_name=name or author.name,
                content="Team update", likes_count=3, comments_count=0,
                created_at=now - timedelta(minutes=age),
            )

        hr_post = post(hr)
        own_post = post(own)
        db.add_all([hr_post, own_post, post(own, "Old assistant name", age=10), post(other),
                    post(own, "Human namesake", author_type="human"),
                    post(other, "Foreign author", tenant_id=foreign.id)])
        db.add(PlazaComment(post_id=hr_post.id, author_id=hr.id, author_type="agent",
                            author_name=hr.name, content="Welcome"))
        await db.commit()

    monkeypatch.setattr(plaza, "tenant_scoped_session", lambda tid: tenant_scoped_session(
        tid, session_factory=app_user_sessionmaker))
    return {"tenant": tenant, "user": users[0], "own": own, "other": other, "hr": hr,
            "hr_post": hr_post, "own_post": own_post}


@pytest.mark.parametrize("display_name, expected", [
    ("HR Onboarding Agent", "HR Onboarding Agent"),
    ("__system_hr__", "HR assistant"),
    ("", "HR assistant"),
])
async def test_read_tool_uses_hr_display_name_in_posts_and_comments(
    plaza_seed, owner_sessionmaker, display_name, expected,
):
    async with owner_sessionmaker() as db:
        participant = await db.get(Participant, plaza_seed["hr"].participant_id)
        participant.display_name = display_name
        await db.commit()

    result = await plaza._plaza_get_new_posts(plaza_seed["own"].id, {})

    assert "__system_hr__" not in result
    assert f"🤖 **{expected}**" in result
    assert f"└─ 🤖 {expected}: Welcome" in result
    assert f"[post_id: {plaza_seed['hr_post'].id}]" in result
    assert "🏛️ Agent Plaza — Recent Posts:" in result
    assert "Team update\n❤️ 3" in result
    assert "**My Assistant**" in result
    assert "Foreign author" not in result


async def test_comment_receipt_also_uses_hr_display_name(plaza_seed, app_user_sessionmaker):
    result = await plaza._plaza_add_comment(plaza_seed["own"].id, {
        "post_id": str(plaza_seed["hr_post"].id), "content": "Thanks",
    })

    assert result == "✅ Comment added to post by HR Onboarding Agent."
    async with tenant_scoped_session(plaza_seed["tenant"].id, session_factory=app_user_sessionmaker) as db:
        comments = list(await db.scalars(select(PlazaComment).where(
            PlazaComment.post_id == plaza_seed["hr_post"].id)))
    assert [(c.author_name, c.content) for c in comments] == [("__system_hr__", "Welcome"),
                                                            ("My Assistant", "Thanks")]


async def test_stats_counts_ids_and_types_not_shared_or_historical_names(plaza_seed, app_user_sessionmaker):
    app = FastAPI()
    app.include_router(plaza_api.router)
    app.dependency_overrides[get_current_user] = lambda: plaza_seed["user"]

    async def db_session():
        async with app_user_sessionmaker() as db:
            yield db

    app.dependency_overrides[get_db] = db_session
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        response = await client.get("/plaza/stats")
    assert response.status_code == 200
    stats = response.json()
    assert stats["total_posts"] == 5
    assert stats["total_comments"] == 1
    rows = stats["top_contributors"]
    assert len(rows) == 4
    assert sum(row["type"] == "agent" and row["name"] == "My Assistant" for row in rows) == 2
    by_author = {(row["author_id"], row["type"]): row for row in rows}
    assert by_author[(str(plaza_seed["own"].id), "agent")] == {
        "author_id": str(plaza_seed["own"].id), "type": "agent", "name": "My Assistant", "posts": 2,
    }
    assert by_author[(str(plaza_seed["other"].id), "agent")] == {
        "author_id": str(plaza_seed["other"].id), "type": "agent", "name": "My Assistant", "posts": 1,
    }
    assert by_author[(str(plaza_seed["own"].id), "human")]["name"] == "Human namesake"
    assert by_author[(str(plaza_seed["hr"].id), "agent")]["name"] == "__system_hr__"
