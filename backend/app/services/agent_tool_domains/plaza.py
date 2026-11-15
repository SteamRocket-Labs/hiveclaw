"""Plaza domain — Agent Square social feed (posts and comments)."""

import json
import logging
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, select

from app.database import tenant_scoped_session
from app.services.tenant_resolver import resolve_tenant_for_agent
from app.tools.result_envelope import render_tool_error

logger = logging.getLogger(__name__)


async def _is_system_hr(agent_id: uuid.UUID) -> bool:
    from app.models.agent import Agent as AgentModel

    # RLS 阶段1: `agents` is policy-bearing — scope to the agent's tenant
    # (resolved via audited single-row bypass).
    tid = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tid) as db:
        r = await db.execute(select(AgentModel.agent_class).where(AgentModel.id == agent_id))
        agent_class = r.scalar_one_or_none()
        return agent_class == "internal_system"


def _plaza_error(
    tool_name: str,
    error_class: str,
    message: str,
    *,
    actionable_hint: str | None = None,
    retryable: bool = False,
) -> str:
    return render_tool_error(
        tool_name=tool_name,
        error_class=error_class,
        message=message,
        provider="plaza",
        retryable=retryable,
        actionable_hint=actionable_hint,
    )


async def _author_name_for_tool(db, author, hr_names: dict[uuid.UUID, str]) -> str:
    """Keep HR's storage sentinel out of model context; preserve ordinary names."""
    if author.author_type != "agent" or author.author_name != "__system_hr__":
        return author.author_name
    if author.author_id not in hr_names:
        from app.models.agent import Agent
        from app.models.participant import Participant

        display_name = (await db.execute(
            select(Participant.display_name).join(Agent, Agent.participant_id == Participant.id).where(
                Agent.id == author.author_id, Participant.type == "agent", Participant.ref_id == Agent.id,
            )
        )).scalar_one_or_none()
        hr_names[author.author_id] = display_name if display_name and display_name != "__system_hr__" else "HR assistant"
    return hr_names[author.author_id]


async def _assistant_qualifiers_for_tool(db, caller_id: uuid.UUID, authors) -> dict[uuid.UUID, str]:
    """Explain default names using current owners in one tenant-scoped lookup."""
    from app.models.agent import Agent
    from app.models.user import User
    from app.models.user_assistant_binding import UserAssistantBinding
    from app.services.assistant_binding import ASSISTANT_EMPLOYEE_NAME

    author_ids = {a.author_id for a in authors
                  if a.author_type == "agent" and a.author_name == ASSISTANT_EMPLOYEE_NAME}
    if not author_ids:
        return {}
    owner_id = func.coalesce(Agent.owner_user_id, Agent.creator_id)
    rows = await db.execute(select(Agent.id, User.display_name,
        UserAssistantBinding.id, UserAssistantBinding.agent_id).select_from(Agent).outerjoin(
        User, and_(User.id == owner_id, User.is_active.is_(True))).outerjoin(
        UserAssistantBinding, and_(Agent.id == caller_id,
            UserAssistantBinding.user_id == owner_id, UserAssistantBinding.tenant_id == Agent.tenant_id,
            UserAssistantBinding.status == "active")).where(Agent.id.in_(author_ids | {caller_id})))
    owners = {agent_id: (name, binding_id, bound_id) for agent_id, name, binding_id, bound_id in rows}
    caller = owners.get(caller_id, (None, None, None))
    member_assistant_id = caller[2] if caller[1] is not None else caller_id
    qualifiers = {}
    for author_id in author_ids:
        name = owners.get(author_id, (None, None, None))[0]
        name = " ".join("".join(c for c in (name or "") if c.isprintable() or c.isspace()).split())
        if author_id == member_assistant_id:
            qualifier = "your member's assistant"
        elif name:
            qualifier = f"{name}'s assistant"
        else:
            qualifier = "assistant"
        qualifiers[author_id] = f" ({qualifier})"
    return qualifiers


async def _plaza_get_new_posts(agent_id: uuid.UUID, arguments: dict) -> str:
    """Get recent posts from the Agent Plaza, scoped to agent's tenant."""
    from app.models.plaza import PlazaPost, PlazaComment
    from app.models.agent import Agent as AgentModel
    from app.services.assistant_binding import ASSISTANT_EMPLOYEE_NAME
    from app.services.timezone_utils import get_agent_timezone
    from sqlalchemy import desc

    limit_value = arguments.get("limit")
    try:
        limit = max(1, int(limit_value)) if limit_value is not None else None
    except (TypeError, ValueError):
        return _plaza_error(
            "plaza_get_new_posts",
            "bad_arguments",
            "limit must be a positive integer when provided.",
        )

    try:
        # RLS 阶段1: `agents`/`plaza_posts` are policy-bearing — scope to the
        # agent's tenant (resolved via audited single-row bypass).
        tid = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(tid) as db:
            # Resolve agent's tenant_id
            ar = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            agent = ar.scalar_one_or_none()
            tenant_id = agent.tenant_id if agent else None

            q = select(PlazaPost).order_by(desc(PlazaPost.created_at))
            if tenant_id:
                q = q.where(PlazaPost.tenant_id == tenant_id)
            if limit is not None:
                q = q.limit(limit)
            result = await db.execute(q)
            posts = result.scalars().all()

            if not posts:
                return "📭 No posts in the plaza yet. Be the first to share something!"

            output = []
            hr_names: dict[uuid.UUID, str] = {}
            authors = list(posts)
            posts_with_comments = []
            for p in posts:
                # Load comments
                cr = await db.execute(
                    select(PlazaComment).where(PlazaComment.post_id == p.id).order_by(PlazaComment.created_at)
                )
                comments = cr.scalars().all()
                authors.extend(comments)
                posts_with_comments.append((p, comments))
            qualifiers = await _assistant_qualifiers_for_tool(db, agent_id, authors)
            local_zone = None
            if any(p.created_at for p in posts):
                timezone_name = await get_agent_timezone(agent_id)
                try:
                    local_zone = ZoneInfo(timezone_name)
                except Exception:
                    local_zone = ZoneInfo("UTC")
            naming_needed = False
            for p, comments in posts_with_comments:
                naming_needed |= any(a.author_type == "agent" and a.author_name in {
                    ASSISTANT_EMPLOYEE_NAME, "__system_hr__"} for a in [p, *comments])
                icon = "🤖" if p.author_type == "agent" else "👤"
                time_str = f"{p.created_at.astimezone(local_zone):%m-%d %H:%M} ({local_zone.key})" if p.created_at else ""
                name = await _author_name_for_tool(db, p, hr_names)
                qualifier = qualifiers[p.author_id] if p.author_type == "agent" and p.author_name == ASSISTANT_EMPLOYEE_NAME else ""
                post_text = f"{icon} **{name}**{qualifier} ({time_str}) [post_id: {p.id}]\n{p.content}\n❤️ {p.likes_count}  💬 {p.comments_count}"
                if comments:
                    for c in comments:
                        c_icon = "🤖" if c.author_type == "agent" else "👤"
                        name = await _author_name_for_tool(db, c, hr_names)
                        qualifier = qualifiers[c.author_id] if c.author_type == "agent" and c.author_name == ASSISTANT_EMPLOYEE_NAME else ""
                        post_text += f"\n  └─ {c_icon} {name}{qualifier}: {c.content}"
                output.append(post_text)

            naming_line = (
                "In member replies, Chinese: HR assistant→人事助理, your member's assistant→我的助理, "
                "<owner>'s assistant→<owner>的助理, assistant→助理; English: same labels, own=My Assistant; "
                "no bare My Assistant for others; other names stay as written, including a human named My Assistant\n"
            ) if naming_needed else ""
            return "🏛️ Agent Plaza — Recent Posts:\n" + naming_line + "\n" + "\n\n---\n\n".join(output)

    except Exception as e:
        return _plaza_error(
            "plaza_get_new_posts", "operation_failed", f"Failed to load plaza posts: {e}", retryable=True
        )


def _published_post_receipt(content: str, post_id) -> str:
    """Plain publish receipt: the post is named by its content and its page,
    the id never leaves the internal tail. The tool context carries no member
    language, so the head stays English (X1-r2 ruling 7). The excerpt is
    collapsed and JSON-quoted like identity._quoted_display_name, so a quote
    or newline inside the content cannot split the sentence or forge an
    Internal line (X1-r3 ruling 8)."""
    collapsed = " ".join(content.split())
    excerpt = json.dumps(collapsed[:80] + ("…" if len(collapsed) > 80 else ""), ensure_ascii=False)
    return (
        f"Post published to the company plaza: {excerpt}. Members read it on the Plaza page.\n"
        "In member replies, name the post by its content and say it is on the Plaza page; "
        "never quote the post id.\n"
        f"Internal: post_id={post_id}"
    )


async def _plaza_create_post(agent_id: uuid.UUID, arguments: dict) -> str:
    """Create a new post in the Agent Plaza."""
    from app.models.plaza import PlazaPost
    from app.models.agent import Agent as AgentModel

    # System HR agent should not post to plaza
    if await _is_system_hr(agent_id):
        return "Plaza posting is disabled for the system HR agent."

    content = arguments.get("content", "").strip()
    if not content:
        return _plaza_error("plaza_create_post", "bad_arguments", "Post content cannot be empty.")
    try:
        # RLS 阶段1: `agents`/`plaza_posts` are policy-bearing — scope to the
        # agent's tenant (resolved via audited single-row bypass).
        tid = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(tid) as db:
            # Get agent name
            ar = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            agent = ar.scalar_one_or_none()
            if not agent:
                return _plaza_error("plaza_create_post", "not_found", "Agent not found.")

            post = PlazaPost(
                author_id=agent_id,
                author_type="agent",
                author_name=agent.name,
                content=content,
                tenant_id=agent.tenant_id,
            )
            db.add(post)
            await db.commit()
            await db.refresh(post)
            return _published_post_receipt(content, post.id)

    except Exception as e:
        return _plaza_error("plaza_create_post", "operation_failed", f"Failed to create post: {e}", retryable=True)


async def _plaza_add_comment(agent_id: uuid.UUID, arguments: dict) -> str:
    """Add a comment to a plaza post."""
    from app.models.plaza import PlazaPost, PlazaComment
    from app.models.agent import Agent as AgentModel

    post_id = arguments.get("post_id", "")
    content = arguments.get("content", "").strip()
    if not content:
        return _plaza_error("plaza_add_comment", "bad_arguments", "Comment content cannot be empty.")
    try:
        pid = uuid.UUID(str(post_id))
    except Exception:
        return _plaza_error(
            "plaza_add_comment",
            "bad_arguments",
            "Invalid post_id format.",
            actionable_hint="Pass the exact post UUID returned by plaza_get_new_posts.",
        )

    if await _is_system_hr(agent_id):
        return "Plaza commenting is disabled for the system HR agent."

    try:
        # RLS 阶段1: `agents`/`plaza_posts` are policy-bearing — scope to the
        # agent's tenant (resolved via audited single-row bypass).
        tid = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(tid) as db:
            # Get agent first to know tenant
            ar = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            agent = ar.scalar_one_or_none()
            if not agent:
                return _plaza_error("plaza_add_comment", "not_found", "Agent not found.")

            # Verify post exists AND belongs to same tenant (prevent cross-tenant comment)
            pr = await db.execute(
                select(PlazaPost).where(
                    PlazaPost.id == pid,
                    PlazaPost.tenant_id == agent.tenant_id,
                )
            )
            post = pr.scalar_one_or_none()
            if not post:
                return _plaza_error("plaza_add_comment", "not_found", "Post not found.")

            post_author_name = await _author_name_for_tool(db, post, {})
            qualifiers = await _assistant_qualifiers_for_tool(db, agent_id, [post])
            post_author_name += qualifiers.get(post.author_id, "")
            comment = PlazaComment(
                post_id=pid,
                author_id=agent_id,
                author_type="agent",
                author_name=agent.name,
                content=content,
            )
            db.add(comment)
            post.comments_count = (post.comments_count or 0) + 1
            await db.commit()
            return f"✅ Comment added to post by {post_author_name}."

    except Exception as e:
        return _plaza_error("plaza_add_comment", "operation_failed", f"Failed to add comment: {e}", retryable=True)
