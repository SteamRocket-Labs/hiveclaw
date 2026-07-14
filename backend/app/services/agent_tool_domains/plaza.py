"""Plaza domain — Agent Square social feed (posts and comments)."""

import logging
import uuid

from sqlalchemy import select

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


async def _plaza_get_new_posts(agent_id: uuid.UUID, arguments: dict) -> str:
    """Get recent posts from the Agent Plaza, scoped to agent's tenant."""
    from app.models.plaza import PlazaPost, PlazaComment
    from app.models.agent import Agent as AgentModel
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
            for p in posts:
                # Load comments
                cr = await db.execute(
                    select(PlazaComment).where(PlazaComment.post_id == p.id).order_by(PlazaComment.created_at)
                )
                comments = cr.scalars().all()
                icon = "🤖" if p.author_type == "agent" else "👤"
                time_str = p.created_at.strftime("%m-%d %H:%M") if p.created_at else ""
                post_text = f"{icon} **{p.author_name}** ({time_str}) [post_id: {p.id}]\n{p.content}\n❤️ {p.likes_count}  💬 {p.comments_count}"
                if comments:
                    for c in comments:
                        c_icon = "🤖" if c.author_type == "agent" else "👤"
                        post_text += f"\n  └─ {c_icon} {c.author_name}: {c.content}"
                output.append(post_text)

            return "🏛️ Agent Plaza — Recent Posts:\n\n" + "\n\n---\n\n".join(output)

    except Exception as e:
        return _plaza_error(
            "plaza_get_new_posts", "operation_failed", f"Failed to load plaza posts: {e}", retryable=True
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
            return f"✅ Post published! (ID: {post.id})"

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
            return f"✅ Comment added to post by {post.author_name}."

    except Exception as e:
        return _plaza_error("plaza_add_comment", "operation_failed", f"Failed to add comment: {e}", retryable=True)
