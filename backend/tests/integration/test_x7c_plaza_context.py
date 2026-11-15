"""X7c: model-facing plaza context preserves names and explains their display."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import event, select, update

from app.models.agent import Agent
from app.models.participant import Participant
from app.models.plaza import PlazaComment, PlazaPost
from app.models.tenant import Tenant
from app.models.user import User
from app.models.user_assistant_binding import UserAssistantBinding
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
        same_owner = Agent(name="My Assistant", tenant_id=own.tenant_id, creator_id=own.creator_id)
        db.add(same_owner)
        await db.flush()
        same_owner_post = PlazaPost(tenant_id=own.tenant_id, author_id=same_owner.id, author_type="agent",
            author_name="My Assistant", content="Same-owner update", likes_count=0, comments_count=0,
            created_at=datetime(2026, 11, 14, 18, 30, tzinfo=timezone.utc))
        other_post = await db.scalar(select(PlazaPost).where(PlazaPost.tenant_id == own.tenant_id,
            PlazaPost.author_id == other.id, PlazaPost.author_type == "agent"))
        binding = UserAssistantBinding(tenant_id=own.tenant_id, user_id=own.creator_id, agent_id=own.id,
            status="active", source="explicit_selection", requester_user_id=own.creator_id,
            owner_user_id_at_bind=own.creator_id)
        db.add_all([same_owner_post, binding, *[
            PlazaComment(post_id=plaza_seed["hr_post"].id, author_id=author.id,
                         author_type=kind, author_name=name, content=content)
            for author, kind, name, content in [
                (other, "agent", "My Assistant", "Other comment"),
                (own, "agent", "My Assistant", "Own comment"),
                (own, "agent", "Researcher", "Ordinary comment"),
                (own, "human", "My Assistant", "Human comment"),
            ]
        ]])
        await db.commit()
    return {**plaza_seed, "own": own, "other": other, "other_post": other_post, "binding": binding,
            "same_owner": same_owner, "same_owner_post": same_owner_post}


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
    assert "└─ 🤖 My Assistant (张三's assistant): Other comment" in result
    assert "└─ 🤖 My Assistant (your member's assistant): Own comment" in result
    assert "└─ 🤖 Researcher: Ordinary comment" in result
    assert "└─ 👤 My Assistant: Human comment" in result
    assert "**Old assistant name**" in result
    assert "👤 **Human namesake**" in result
    assert "Foreign author" not in result
    assert "__system_hr__" not in result
    assert f"[post_id: {plaza_context['own_post'].id}]" in result
    # The owner lookup is explicitly required to be batched per tool call.
    assert len(statements) == 1


@pytest.mark.parametrize("owner_state", ["blank", "inactive", "controls"])
async def test_unknown_owner_display_uses_a_neutral_assistant_qualifier(
    plaza_context, owner_sessionmaker, owner_state,
):
    async with owner_sessionmaker() as db:
        other = await db.get(Agent, plaza_context["other"].id)
        owner = await db.get(User, other.owner_user_id)
        if owner_state == "inactive":
            owner.is_active = False
        else:
            owner.display_name = "" if owner_state == "blank" else " \n\t\x1b\u200b "
        await db.commit()

    result = await plaza._plaza_get_new_posts(plaza_context["own"].id, {})

    line = next(line for line in result.splitlines() if f"[post_id: {plaza_context['other_post'].id}]" in line)
    assert line.startswith("🤖 **My Assistant** (assistant) (")
    assert "└─ 🤖 My Assistant (assistant): Other comment" in result
    assert "another member's assistant" not in result
    assert "张三's assistant" not in result
    assert "\x1b" not in result and "\u200b" not in result


@pytest.mark.parametrize("binding_target, caller", [
    ("own", "own"), ("own", "same_owner"), ("same_owner", "own"),
    (None, "own"), ("provisioning", "own"),
])
async def test_only_the_active_bound_assistant_gets_the_members_label(
    plaza_context, owner_sessionmaker, binding_target, caller,
):
    async with owner_sessionmaker() as db:
        binding = await db.get(UserAssistantBinding, plaza_context["binding"].id)
        if binding_target is None:
            await db.delete(binding)
        elif binding_target == "provisioning":
            binding.status, binding.agent_id = "provisioning", plaza_context["same_owner"].id
        else:
            binding.agent_id = plaza_context[binding_target].id
        await db.commit()

    result = await plaza._plaza_get_new_posts(plaza_context[caller].id, {})

    own_key = binding_target if binding_target in {"own", "same_owner"} else caller
    for key, post_key in [("own", "own_post"), ("same_owner", "same_owner_post")]:
        line = next(line for line in result.splitlines() if f"[post_id: {plaza_context[post_key].id}]" in line)
        qualifier = "your member's assistant" if key == own_key else "李四's assistant"
        assert line.startswith(f"🤖 **My Assistant** ({qualifier}) (")
    own_comment_qualifier = "your member's assistant" if own_key == "own" else "李四's assistant"
    assert f"└─ 🤖 My Assistant ({own_comment_qualifier}): Own comment" in result


async def test_active_owner_name_is_one_printable_line(plaza_context, owner_sessionmaker):
    async with owner_sessionmaker() as db:
        owner = await db.get(User, plaza_context["other"].owner_user_id)
        owner.display_name = "  张三 \n 李\t四\x1b\u200b  "
        await db.commit()

    result = await plaza._plaza_get_new_posts(plaza_context["own"].id, {})

    assert "🤖 **My Assistant** (张三 李 四's assistant) (" in result
    assert "└─ 🤖 My Assistant (张三 李 四's assistant): Other comment" in result
    assert "\x1b" not in result and "\u200b" not in result and "\t" not in result


async def test_naming_line_precedes_posts_and_covers_both_languages(plaza_context):
    result = await plaza._plaza_get_new_posts(plaza_context["own"].id, {})

    rule, posts = result.split("\n\n", 1)
    assert rule == (
        "🏛️ Agent Plaza — Recent Posts:\n"
        "In member replies, Chinese: HR assistant→人事助理, your member's assistant→我的助理, "
        "<owner>'s assistant→<owner>的助理, assistant→助理; English: same labels, own=My Assistant; "
        "no bare My Assistant for others; other names stay as written, including a human named My Assistant"
    )
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
            (await db.get(PlazaPost, plaza_context["hr_post"].id)).author_name = "__system_hr__"
        elif special in {"hr_comment", "assistant_comment"}:
            comments = list(await db.scalars(select(PlazaComment).where(
                PlazaComment.post_id == plaza_context["hr_post"].id)))
            target_id = plaza_context["hr" if special == "hr_comment" else "other"].id
            target = next(comment for comment in comments if comment.author_id == target_id)
            target.author_name = "__system_hr__" if special == "hr_comment" else "My Assistant"
        await db.commit()

    result = await plaza._plaza_get_new_posts(plaza_context["own"].id, {})

    assert ("In member replies" in result) == (special != "none")
    if special == "none":
        assert (f"🤖 **Researcher** (11-15 02:30 (Asia/Shanghai)) "
                f"[post_id: {plaza_context['own_post'].id}]") in result
    if special == "human":
        assert "other names stay as written, including a human named My Assistant" in result
        assert (f"👤 **My Assistant** (11-15 02:30 (Asia/Shanghai)) "
                f"[post_id: {plaza_context['own_post'].id}]") in result
        assert "👤 **My Assistant** (your member's assistant)" not in result
    if special == "assistant_comment":
        assert "└─ 🤖 My Assistant (张三's assistant): Other comment" in result


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


@pytest.mark.parametrize("agent_zone, tenant_zone", [("CST", "Asia/Shanghai"), (None, "GMT+8")])
async def test_invalid_timezone_keeps_posts_readable_in_utc(
    plaza_context, owner_sessionmaker, agent_zone, tenant_zone,
):
    async with owner_sessionmaker() as db:
        (await db.get(Agent, plaza_context["own"].id)).timezone = agent_zone
        (await db.get(Tenant, plaza_context["tenant"].id)).timezone = tenant_zone
        await db.commit()

    result = await plaza._plaza_get_new_posts(plaza_context["own"].id, {})

    assert f"(11-14 18:30 (UTC)) [post_id: {plaza_context['own_post'].id}]" in result
    assert "Team update" in result


async def test_comment_receipt_qualifies_the_default_post_author(plaza_context):
    result = await plaza._plaza_add_comment(plaza_context["other"].id, {
        "post_id": str(plaza_context["own_post"].id), "content": "Thanks",
    })

    assert result == "✅ Comment added to post by My Assistant (李四's assistant)."
