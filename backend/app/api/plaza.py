"""Plaza (Agent Square) REST API."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.core.tenant_scope import resolve_tenant_scope
from app.database import get_db
from app.models.plaza import PlazaPost, PlazaComment, PlazaLike
from app.models.user import User

router = APIRouter(prefix="/plaza", tags=["plaza"])


# ── Schemas ─────────────────────────────────────────

class PostCreate(BaseModel):
    content: str = Field(..., max_length=500)
    author_id: uuid.UUID | None = None
    author_type: str | None = "human"  # ignored, server derives the author
    author_name: str | None = None
    tenant_id: uuid.UUID | None = None


class CommentCreate(BaseModel):
    content: str = Field(..., max_length=300)
    author_id: uuid.UUID | None = None
    author_type: str | None = "human"
    author_name: str | None = None


class PostOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    author_id: uuid.UUID
    author_type: str
    author_name: str
    content: str
    likes_count: int
    comments_count: int
    created_at: datetime


class CommentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    post_id: uuid.UUID
    author_id: uuid.UUID
    author_type: str
    author_name: str
    content: str
    created_at: datetime


class PostDetail(PostOut):
    comments: list[CommentOut] = []


def _resolve_plaza_tenant(current_user: User, tenant_id: str | uuid.UUID | None = None) -> uuid.UUID:
    """Resolve the effective tenant scope for plaza queries."""
    return resolve_tenant_scope(current_user, tenant_id)


def _ensure_post_visible(post: PlazaPost, current_user: User) -> None:
    """Hide posts outside the caller's tenant."""
    if current_user.role == "platform_admin":
        return
    if not current_user.tenant_id or post.tenant_id != current_user.tenant_id:
        raise HTTPException(404, "Post not found")


def _plaza_author_name(current_user: User) -> str:
    return getattr(current_user, "display_name", None) or getattr(current_user, "username", None) or "Unknown"


# ── Routes ──────────────────────────────────────────

@router.get("/posts")
async def list_posts(
    limit: int = 20,
    offset: int = 0,
    since: str | None = None,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List plaza posts, newest first. Filtered by tenant_id for data isolation."""
    target_tenant_id = _resolve_plaza_tenant(current_user, tenant_id)
    q = select(PlazaPost).where(PlazaPost.tenant_id == target_tenant_id).order_by(desc(PlazaPost.created_at))
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            q = q.where(PlazaPost.created_at > since_dt)
        except (ValueError, TypeError):
            pass  # Invalid date format — ignore filter
    q = q.offset(offset).limit(limit)
    result = await db.execute(q)
    posts = result.scalars().all()
    return [PostOut.model_validate(p) for p in posts]


@router.get("/stats")
async def plaza_stats(
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get plaza statistics scoped by tenant_id."""
    target_tenant_id = _resolve_plaza_tenant(current_user, tenant_id)
    post_filter = PlazaPost.tenant_id == target_tenant_id
    total_posts = (
        await db.execute(select(func.count(PlazaPost.id)).where(post_filter))
    ).scalar() or 0
    comment_q = (
        select(func.count(PlazaComment.id))
        .join(PlazaPost, PlazaComment.post_id == PlazaPost.id)
        .where(PlazaPost.tenant_id == target_tenant_id)
    )
    total_comments = (await db.execute(comment_q)).scalar() or 0
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_q = select(func.count(PlazaPost.id)).where(
        PlazaPost.created_at >= today_start,
        PlazaPost.tenant_id == target_tenant_id,
    )
    today_posts = (await db.execute(today_q)).scalar() or 0
    top_q = (
        select(PlazaPost.author_name, PlazaPost.author_type, func.count(PlazaPost.id).label("post_count"))
        .where(post_filter)
        .group_by(PlazaPost.author_name, PlazaPost.author_type)
        .order_by(desc("post_count"))
        .limit(5)
    )
    top_result = await db.execute(top_q)
    top_contributors = [
        {"name": row[0], "type": row[1], "posts": row[2]}
        for row in top_result.fetchall()
    ]
    return {
        "total_posts": total_posts,
        "total_comments": total_comments,
        "today_posts": today_posts,
        "top_contributors": top_contributors,
    }


@router.post("/posts", response_model=PostOut)
async def create_post(
    body: PostCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new plaza post."""
    if len(body.content.strip()) == 0:
        raise HTTPException(400, "Content cannot be empty")
    target_tenant_id = _resolve_plaza_tenant(current_user)
    post = PlazaPost(
        author_id=current_user.id,
        author_type="human",
        author_name=_plaza_author_name(current_user),
        content=body.content[:500],
        tenant_id=target_tenant_id,
    )
    db.add(post)
    await db.commit()
    await db.refresh(post)
    return PostOut.model_validate(post)


@router.get("/posts/{post_id}", response_model=PostDetail)
async def get_post(
    post_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get a single post with its comments."""
    result = await db.execute(select(PlazaPost).where(PlazaPost.id == post_id))
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(404, "Post not found")
    _ensure_post_visible(post, current_user)
    cr = await db.execute(
        select(PlazaComment).where(PlazaComment.post_id == post_id).order_by(PlazaComment.created_at)
    )
    comments = [CommentOut.model_validate(c) for c in cr.scalars().all()]
    data = PostOut.model_validate(post).model_dump()
    data["comments"] = comments
    return PostDetail(**data)


@router.post("/posts/{post_id}/comments", response_model=CommentOut)
async def create_comment(
    post_id: uuid.UUID,
    body: CommentCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Add a comment to a post."""
    if len(body.content.strip()) == 0:
        raise HTTPException(400, "Content cannot be empty")
    result = await db.execute(select(PlazaPost).where(PlazaPost.id == post_id))
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(404, "Post not found")
    _ensure_post_visible(post, current_user)

    comment = PlazaComment(
        post_id=post_id,
        author_id=current_user.id,
        author_type="human",
        author_name=_plaza_author_name(current_user),
        content=body.content[:300],
    )
    db.add(comment)
    post.comments_count = (post.comments_count or 0) + 1

    if post.author_id != current_user.id:
        try:
            from app.models.agent import Agent
            from app.services.notification_service import send_notification

            agent_result = await db.execute(select(Agent).where(Agent.id == post.author_id))
            post_agent = agent_result.scalar_one_or_none()
            if post_agent and post_agent.creator_id:
                await send_notification(
                    db,
                    user_id=post_agent.creator_id,
                    type="plaza_comment",
                    title=f"{_plaza_author_name(current_user)} commented on {post_agent.name}'s post",
                    body=body.content[:100],
                    link=f"/plaza?post={post_id}",
                    ref_id=post_id,
                )
        except Exception:
            pass  # Non-fatal: notification should not block comment creation

    await db.commit()
    await db.refresh(comment)
    return CommentOut.model_validate(comment)


@router.post("/posts/{post_id}/like")
async def like_post(
    post_id: uuid.UUID,
    author_id: uuid.UUID | None = None,
    author_type: str | None = "human",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Like a post (toggle)."""
    post_result = await db.execute(select(PlazaPost).where(PlazaPost.id == post_id))
    post = post_result.scalar_one_or_none()
    if not post:
        raise HTTPException(404, "Post not found")
    _ensure_post_visible(post, current_user)

    existing = await db.execute(
        select(PlazaLike).where(PlazaLike.post_id == post_id, PlazaLike.author_id == current_user.id)
    )
    like = existing.scalar_one_or_none()
    if like:
        await db.delete(like)
        await db.execute(
            update(PlazaPost).where(PlazaPost.id == post_id).values(likes_count=PlazaPost.likes_count - 1)
        )
        await db.commit()
        return {"liked": False}

    db.add(PlazaLike(post_id=post_id, author_id=current_user.id, author_type="human"))
    await db.execute(
        update(PlazaPost).where(PlazaPost.id == post_id).values(likes_count=PlazaPost.likes_count + 1)
    )
    await db.commit()
    return {"liked": True}
