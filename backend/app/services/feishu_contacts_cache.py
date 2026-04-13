"""Helpers for loading and writing per-agent Feishu contact cache."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from app.config import get_settings


def get_feishu_contact_cache_paths(agent_id: uuid.UUID) -> list[Path]:
    """Return canonical and legacy cache file paths for an agent."""
    safe_agent_id = str(agent_id).replace("..", "").replace("/", "")
    canonical = Path(get_settings().AGENT_DATA_DIR) / safe_agent_id / "workspace" / "feishu_contacts_cache.json"
    legacy = Path("/data/workspaces") / safe_agent_id / "feishu_contacts_cache.json"
    if canonical == legacy:
        return [canonical]
    return [canonical, legacy]


def load_feishu_contacts_cache(agent_id: uuid.UUID) -> dict:
    """Load cached Feishu contacts from canonical path, then legacy fallback."""
    for path in get_feishu_contact_cache_paths(agent_id):
        try:
            if not path.exists():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            users = payload.get("users", [])
            if isinstance(users, list):
                return {
                    "ts": payload.get("ts"),
                    "users": users,
                    "path": path,
                }
        except Exception:
            continue
    return {"ts": None, "users": [], "path": get_feishu_contact_cache_paths(agent_id)[0]}


def upsert_feishu_contact_cache(
    agent_id: uuid.UUID,
    *,
    open_id: str,
    user_id: str,
    name: str,
    email: str,
) -> Path | None:
    """Write/update one cached contact in the canonical agent workspace cache."""
    if not open_id or not name:
        return None

    path = get_feishu_contact_cache_paths(agent_id)[0]
    path.parent.mkdir(parents=True, exist_ok=True)

    existing = load_feishu_contacts_cache(agent_id)
    users_by_key: dict[str, dict] = {}
    for cached in existing.get("users", []):
        cache_key = cached.get("user_id") or cached.get("open_id", "")
        if cache_key:
            users_by_key[cache_key] = cached

    users_by_key[user_id or open_id] = {
        "open_id": open_id,
        "user_id": user_id,
        "name": name,
        "email": email,
    }
    path.write_text(
        json.dumps(
            {
                "ts": time.time(),
                "users": list(users_by_key.values()),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    return path
