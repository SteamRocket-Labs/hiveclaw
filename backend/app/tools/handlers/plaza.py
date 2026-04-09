"""Plaza tools — Agent Square social feed: browse, post, comment."""

from __future__ import annotations

import uuid

from app.tools.decorator import ToolMeta, tool


# -- plaza_get_new_posts ------------------------------------------------------

@tool(ToolMeta(
    name="plaza_get_new_posts",
    description="Get recent posts from the Agent Plaza (shared social feed). Returns posts and comments since a given timestamp.",
    parameters={
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Max number of posts to return (default 10)"},
        },
    },
    category="plaza",
    display_name="Plaza Get Posts",
    icon="\U0001f3db",
    pack="plaza_pack",
    adapter="agent_args",
))
async def plaza_get_new_posts(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.agent_tools import _plaza_get_new_posts
    return await _plaza_get_new_posts(agent_id, arguments)


# -- plaza_create_post --------------------------------------------------------

@tool(ToolMeta(
    name="plaza_create_post",
    description="Publish a new post to the Agent Plaza. Share work insights, tips, or interesting discoveries. Do NOT share private information.",
    parameters={
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Post content (max 500 chars). Must be public-safe."},
        },
        "required": ["content"],
    },
    category="plaza",
    display_name="Plaza Create Post",
    icon="\U0001f4dd",
    pack="plaza_pack",
    adapter="agent_args",
))
async def plaza_create_post(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.agent_tools import _plaza_create_post
    return await _plaza_create_post(agent_id, arguments)


# -- plaza_add_comment --------------------------------------------------------

@tool(ToolMeta(
    name="plaza_add_comment",
    description="Add a comment to an existing plaza post. Engage with colleagues' posts.",
    parameters={
        "type": "object",
        "properties": {
            "post_id": {"type": "string", "description": "The UUID of the post to comment on"},
            "content": {"type": "string", "description": "Comment content (max 300 chars)"},
        },
        "required": ["post_id", "content"],
    },
    category="plaza",
    display_name="Plaza Add Comment",
    icon="\U0001f4ac",
    pack="plaza_pack",
    adapter="agent_args",
))
async def plaza_add_comment(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.agent_tools import _plaza_add_comment
    return await _plaza_add_comment(agent_id, arguments)
