"""Feishu users — user search and contacts cache management."""

from __future__ import annotations

import logging
import uuid

from app.services.agent_tool_domains.feishu_helpers import _get_feishu_token

logger = logging.getLogger(__name__)


def _fuzzy_score(query: str, name: str) -> float:
    """Score how well *query* matches *name* (0.0–1.0).

    - Exact match → 1.0
    - Substring containment → 0.9
    - All query chars found in order → 0.7 + bonus
    - Partial character overlap → proportion of matched chars × 0.5
    """
    q = query.strip().lower()
    n = name.strip().lower()
    if not q or not n:
        return 0.0
    if q == n:
        return 1.0
    if q in n or n in q:
        return 0.9

    # Ordered subsequence match (e.g. partial name chars found in order)
    qi = 0
    for ch in n:
        if qi < len(q) and ch == q[qi]:
            qi += 1
    if qi == len(q):
        return 0.7 + 0.1 * (len(q) / len(n))

    # Character overlap ratio
    q_chars = set(q)
    n_chars = set(n)
    overlap = len(q_chars & n_chars)
    if overlap > 0:
        return 0.5 * (overlap / len(q_chars))

    return 0.0


def _format_user_result(users: list[dict], query: str, source: str) -> str:
    """Format a list of user dicts into a readable result string."""
    lines = [f"Found {len(users)} user(s) matching \"{query}\" ({source}):\n"]
    for u in users:
        display_name = u.get("name") or u.get("display_name", "")
        en_name = u.get("en_name", "")
        open_id = u.get("open_id", "")
        user_id = u.get("user_id", "") or u.get("feishu_user_id", "")
        email = u.get("email", "")
        dept = u.get("department_path", "")

        lines.append(f"• **{display_name}**{'（' + en_name + '）' if en_name else ''}")
        if user_id:
            lines.append(f"  user_id: `{user_id}`")
        if open_id:
            lines.append(f"  open_id: `{open_id}`")
        if email:
            lines.append(f"  email: {email}")
        if dept:
            lines.append(f"  dept: {dept}")
    return "\n".join(lines)


async def _feishu_user_search(agent_id: uuid.UUID, arguments: dict) -> str:
    """Search for colleagues by name with fuzzy matching.

    Strategy (best match wins):
    1. Local contacts cache (populated when anyone messages the bot).
    2. OrgMember table (populated by org sync) — fuzzy scored.
    3. Platform User table — fuzzy scored.
    """
    import json as _json
    import pathlib as _pl

    name = (arguments.get("name") or "").strip()
    if not name:
        return "❌ Missing required argument 'name'"

    creds = await _get_feishu_token(agent_id)
    if not creds:
        return "❌ Agent has no Feishu channel configured."

    # ── 1. Local contacts cache ──────────────────────────────────────────────
    _cache_file = _pl.Path(f"/data/workspaces/{agent_id}/feishu_contacts_cache.json")
    _cached_users: list[dict] = []
    try:
        if _cache_file.exists():
            _raw = _json.loads(_cache_file.read_text())
            _cached_users = _raw.get("users", [])
    except Exception as e:
        logger.debug("Suppressed: %s", e)

    # Score cached users
    scored_cache = []
    for u in _cached_users:
        best = max(
            _fuzzy_score(name, u.get("name", "")),
            _fuzzy_score(name, u.get("en_name", "")),
        )
        if best >= 0.5:
            scored_cache.append((best, u))

    if scored_cache:
        scored_cache.sort(key=lambda x: x[0], reverse=True)
        return _format_user_result([u for _, u in scored_cache[:5]], name, "cache")

    # ── Resolve agent tenant_id ──────────────────────────────────────────────
    _tenant_id = None
    try:
        from app.database import async_session as _async_session
        from sqlalchemy import select as _sa_select
        from app.models.agent import Agent as _Agent
        async with _async_session() as _db:
            _agent_r = await _db.execute(_sa_select(_Agent.tenant_id).where(_Agent.id == agent_id))
            _tenant_id = _agent_r.scalar_one_or_none()
    except Exception as e:
        logger.debug("Suppressed tenant lookup: %s", e)

    # ── 2. OrgMember table — fuzzy scored ────────────────────────────────────
    try:
        from app.database import async_session as _async_session
        from sqlalchemy import select as _sa_select
        from app.models.org import OrgMember as _OrgMember

        _om_query = _sa_select(_OrgMember)
        if _tenant_id:
            _om_query = _om_query.where(_OrgMember.tenant_id == _tenant_id)
        async with _async_session() as _db:
            _r = await _db.execute(_om_query)
            _all_members = _r.scalars().all()

        scored_members = []
        for _om in _all_members:
            best = max(
                _fuzzy_score(name, _om.name or ""),
                _fuzzy_score(name, getattr(_om, "en_name", "") or ""),
            )
            if best >= 0.3:
                scored_members.append((best, {
                    "name": _om.name,
                    "feishu_user_id": _om.feishu_user_id,
                    "open_id": _om.feishu_open_id,
                    "email": getattr(_om, "email", ""),
                    "department_path": getattr(_om, "department_path", ""),
                }))

        if scored_members:
            scored_members.sort(key=lambda x: x[0], reverse=True)
            return _format_user_result([u for _, u in scored_members[:5]], name, "directory")
    except Exception as e:
        logger.debug("Suppressed OrgMember search: %s", e)

    # ── 3. Platform User table ───────────────────────────────────────────────
    try:
        from app.database import async_session as _async_session
        from sqlalchemy import select as _sa_select
        from app.models.user import User as _User

        _user_query = _sa_select(_User)
        if _tenant_id:
            _user_query = _user_query.where(_User.tenant_id == _tenant_id)
        async with _async_session() as _db:
            _r = await _db.execute(_user_query)
            _all_users = _r.scalars().all()

        scored_users = []
        for _pu in _all_users:
            _uid = getattr(_pu, "feishu_user_id", None)
            _oid = getattr(_pu, "feishu_open_id", None)
            if not (_uid or _oid):
                continue
            best = _fuzzy_score(name, _pu.display_name or "")
            if best >= 0.3:
                scored_users.append((best, {
                    "name": _pu.display_name,
                    "user_id": _uid or "",
                    "open_id": _oid or "",
                    "email": getattr(_pu, "email", "") or "",
                }))

        if scored_users:
            scored_users.sort(key=lambda x: x[0], reverse=True)
            return _format_user_result([u for _, u in scored_users[:5]], name, "users")
    except Exception as e:
        logger.debug("Suppressed User search: %s", e)

    total = len(_cached_users)
    return (
        f"❌ No user matching \"{name}\" found (searched {total} cached + directory + users).\n\n"
        "Suggestions:\n"
        "- Try a shorter query (family name or given name only)\n"
        "- Provide the Feishu open_id or work email directly"
    )


async def _feishu_contacts_refresh(agent_id: uuid.UUID) -> None:
    """Force-clear the local contacts cache so next search re-fetches from API."""
    import pathlib as _pl
    _cache_file = _pl.Path("/data/workspaces") / str(agent_id) / "feishu_contacts_cache.json"
    try:
        if _cache_file.exists():
            _cache_file.unlink()
    except Exception as e:
        logger.debug("Suppressed: %s", e)
