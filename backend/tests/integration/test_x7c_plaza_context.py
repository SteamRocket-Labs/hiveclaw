"""X7c: model-facing plaza context preserves names and explains their display."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import event, select, update

from app.models.agent import Agent
from app.models.participant import Participant
from app.models.plaza import PlazaComment, PlazaPost
from app.models.tenant import Tenant
from app.models.user import User
from app.services.agent_tool_domains import plaza
from tests.integration.test_x7b_plaza_names import plaza_seed


@pytest.fixture
async def plaza_context(plaza_seed, owner_sessionmaker):
    async with owner_sessionmaker() as db:
        own = await db.get(Agent, plaza_seed["own"].id)
        other = await db.get(Agent, plaza_seed["other"].id)
        (await db.get(User, own.creator_id)).display_name = "李四"
        (await db.get(User, other.creator_id)).display_name = "张三"
        # Ownership after handover takes precedence over the original creator.
        other.owner_user_id = other.creator_id
        other.creator_id = own.creator_id
        (await db.get(Tenant, plaza_seed["tenant"].id)).timezone = "Asia/Shanghai"
        (await db.get(Participant, plaza_seed["hr"].participant_id)).display_name = ""
        await db.execute(update(PlazaPost).where(PlazaPost.tenant_id == plaza_seed["tenant"].id).values(
            created_at=datetime(2026, 11, 14, 18, 30, tzinfo=timezone.utc)))
        db.add(PlazaComment(post_id=plaza_seed["hr_post"].id, author_id=other.id,
                            author_type="agent", author_name="My Assistant", content="Other comment"))
        await db.commit()
    return plaza_seed


async def test_default_assistant_posts_identify_own_member_and_current_owner(plaza_context, app_user_engine):
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        if "JOIN users" in statement and "agents" in statement:
            statements.append(statement)

    event.listen(app_user_engine.sync_engine, "before_cursor_execute", capture)
    try:
        result = await plaza._plaza_get_new_posts(plaza_context["own"].id, {})
    finally:
        event.remove(app_user_engine.sync_engine, "before_cursor_execute", capture)

    assert "🤖 **My Assistant** (your member's assistant)" in result
    assert "🤖 **My Assistant** (张三's assistant)" in result
    assert "🤖 **HR assistant**" in result
    assert "└─ 🤖 HR assistant: Welcome" in result
    assert "└─ 🤖 My Assistant: Other comment" in result
    assert "**Old assistant name**" in result
    assert "👤 **Human namesake**" in result
    assert "Foreign author" not in result
    assert "__system_hr__" not in result
    assert f"[post_id: {plaza_context['own_post'].id}]" in result
    # The owner lookup is explicitly required to be batched per tool call.
    assert len(statements) == 1


async def test_unknown_owner_display_uses_a_neutral_assistant_qualifier(plaza_context, owner_sessionmaker):
    async with owner_sessionmaker() as db:
        other = await db.get(Agent, plaza_context["other"].id)
        (await db.get(User, other.owner_user_id)).display_name = ""
        await db.commit()

    result = await plaza._plaza_get_new_posts(plaza_context["own"].id, {})

    assert "🤖 **My Assistant** (another member's assistant)" in result
    assert "🤖 **My Assistant** ('s assistant)" not in result


async def test_naming_line_precedes_posts_and_covers_both_languages(plaza_context):
    result = await plaza._plaza_get_new_posts(plaza_context["own"].id, {})

    rule, posts = result.split("\n\n", 1)
    assert "In member replies" in rule
    assert "人事助理" in rule
    assert "我的助理" in rule
    assert "<owner>的助理" in rule
    assert "助理 if unknown" in rule
    assert "in English" in rule
    assert "<owner>'s assistant" in rule
    assert "never" in rule and "bare My Assistant" in rule
    assert "[post_id:" in posts


@pytest.mark.parametrize("special", ["none", "human", "hr_comment", "assistant_comment"])
async def test_naming_line_only_when_special_agent_authors_appear(plaza_context, owner_sessionmaker, special):
    async with owner_sessionmaker() as db:
        await db.execute(update(PlazaPost).where(PlazaPost.tenant_id == plaza_context["tenant"].id).values(
            author_name="Researcher"))
        comment_ids = select(PlazaPost.id).where(PlazaPost.tenant_id == plaza_context["tenant"].id)
        await db.execute(update(PlazaComment).where(PlazaComment.post_id.in_(comment_ids)).values(
            author_name="Researcher"))
        if special == "human":
            post = await db.get(PlazaPost, plaza_context["own_post"].id)
            post.author_name, post.author_type = "My Assistant", "human"
        elif special in {"hr_comment", "assistant_comment"}:
            comments = list(await db.scalars(select(PlazaComment).where(
                PlazaComment.post_id == plaza_context["hr_post"].id)))
            target_id = plaza_context["hr" if special == "hr_comment" else "other"].id
            target = next(comment for comment in comments if comment.author_id == target_id)
            target.author_name = "__system_hr__" if special == "hr_comment" else "My Assistant"
        await db.commit()

    result = await plaza._plaza_get_new_posts(plaza_context["own"].id, {})

    assert ("In member replies" in result) == (special in {"hr_comment", "assistant_comment"})
    if special == "human":
        assert "👤 **My Assistant** (" in result
        assert "member's assistant)" not in result


@pytest.mark.parametrize("agent_zone, expected", [(None, "11-15 02:30 (Asia/Shanghai)"),
                                                   ("America/Los_Angeles", "11-14 10:30 (America/Los_Angeles)")])
async def test_plaza_times_use_the_calling_agents_effective_timezone(
    plaza_context, owner_sessionmaker, agent_zone, expected,
):
    async with owner_sessionmaker() as db:
        (await db.get(Agent, plaza_context["own"].id)).timezone = agent_zone
        await db.commit()

    result = await plaza._plaza_get_new_posts(plaza_context["own"].id, {})

    assert f"({expected}) [post_id: {plaza_context['own_post'].id}]" in result


async def test_comment_receipt_qualifies_the_default_post_author(plaza_context):
    result = await plaza._plaza_add_comment(plaza_context["other"].id, {
        "post_id": str(plaza_context["own_post"].id), "content": "Thanks",
    })

    assert result == "✅ Comment added to post by My Assistant (李四's assistant)."
