"""Feishu identity maintenance script.

This script:
1. Backfills stable `feishu_user_id` values from tenant-scoped `feishu_org_sync`
   credentials when only `open_id` is present.
2. Refreshes provider-driven `external_identities`.
3. Merges duplicate Feishu users and normalizes old `feishu_p2p_{open_id}` sessions
   into canonical `feishu_p2p_{user_id}` sessions.

Usage:
  Docker:  docker exec hive-backend-1 python3 -m app.scripts.cleanup_duplicate_feishu_users
  Source:  cd backend && python3 -m app.scripts.cleanup_duplicate_feishu_users
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from app.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)


async def _load_tenant_feishu_configs(db) -> dict[uuid.UUID | None, tuple[str, str]]:
    from sqlalchemy import select

    from app.models.tenant_setting import TenantSetting

    configs: dict[uuid.UUID | None, tuple[str, str]] = {}
    result = await db.execute(select(TenantSetting).where(TenantSetting.key == "feishu_org_sync"))
    for setting in result.scalars().all():
        payload = setting.value or {}
        app_id = payload.get("app_id")
        app_secret = payload.get("app_secret")
        if app_id and app_secret:
            configs[setting.tenant_id] = (app_id, app_secret)

    settings = get_settings()
    if settings.FEISHU_APP_ID and settings.FEISHU_APP_SECRET:
        configs[None] = (settings.FEISHU_APP_ID, settings.FEISHU_APP_SECRET)

    return configs


async def _backfill_users(db, configs: dict[uuid.UUID | None, tuple[str, str]]) -> int:
    from sqlalchemy import select

    from app.models.user import User
    from app.services.auth_provider import feishu_auth_provider
    from app.services.feishu_service import feishu_service

    result = await db.execute(
        select(User).where(
            User.feishu_open_id.is_not(None),
            (User.feishu_user_id.is_(None)) | (User.feishu_user_id == ""),
        )
    )
    users = list(result.scalars().all())
    logger.info("Found %s users needing user_id backfill", len(users))

    updated = 0
    for user in users:
        creds = configs.get(user.tenant_id) or configs.get(None)
        if not creds:
            logger.warning("No Feishu credentials for tenant %s; skipping user %s", user.tenant_id, user.username)
            continue

        try:
            user_info = await feishu_service.get_contact_user_by_open_id(
                creds[0],
                creds[1],
                user.feishu_open_id,
                stage="script_backfill_user",
            )
        except Exception as exc:
            logger.warning("Failed to resolve user %s: %s", user.username, exc)
            continue

        if not user_info.get("user_id"):
            logger.warning("No user_id returned for user %s", user.username)
            continue

        user.feishu_user_id = user_info["user_id"]
        if user_info.get("union_id"):
            user.feishu_union_id = user_info["union_id"]
        if user_info.get("email") and user.email.endswith("@feishu.local"):
            user.email = user_info["email"]

        provider = await feishu_auth_provider._ensure_provider(db, user.tenant_id)
        await feishu_auth_provider._upsert_external_identity(
            db,
            provider,
            user,
            {
                "user_id": user_info.get("user_id"),
                "open_id": user_info.get("open_id") or user.feishu_open_id,
                "union_id": user_info.get("union_id"),
                "email": user_info.get("email", ""),
                "mobile": user_info.get("mobile", ""),
                "name": user_info.get("name", "") or user.display_name,
                "avatar_url": user_info.get("avatar_url", "") or user.avatar_url,
                "raw_profile": user_info,
            },
        )
        updated += 1

    await db.flush()
    return updated


async def _backfill_org_members(db, configs: dict[uuid.UUID | None, tuple[str, str]]) -> int:
    from sqlalchemy import select

    from app.models.org import OrgMember
    from app.services.auth_provider import feishu_auth_provider
    from app.services.feishu_service import feishu_service

    result = await db.execute(
        select(OrgMember).where(
            OrgMember.feishu_open_id.is_not(None),
            (OrgMember.feishu_user_id.is_(None)) | (OrgMember.feishu_user_id == ""),
        )
    )
    members = list(result.scalars().all())
    logger.info("Found %s org members needing user_id backfill", len(members))

    updated = 0
    for member in members:
        creds = configs.get(member.tenant_id) or configs.get(None)
        if not creds:
            continue

        try:
            user_info = await feishu_service.get_contact_user_by_open_id(
                creds[0],
                creds[1],
                member.feishu_open_id,
                stage="script_backfill_org_member",
            )
        except Exception as exc:
            logger.warning("Failed to resolve org member %s: %s", member.name, exc)
            continue

        user_id = user_info.get("user_id")
        if not user_id:
            continue

        provider = await feishu_auth_provider._ensure_provider(db, member.tenant_id)
        member.provider_id = provider.id
        member.external_id = user_id
        member.open_id = user_info.get("open_id") or member.feishu_open_id
        member.unionid = user_info.get("union_id")
        member.feishu_user_id = user_id
        updated += 1

    await db.flush()
    return updated


async def main():
    # Import models so SQLAlchemy resolves metadata before async work starts.
    from app.models import (  # noqa: F401
        activity_log, agent, audit, channel_config, chat_session,
        gateway_message, identity, invitation_code, llm, notification, org,
        participant, plaza, skill, system_settings, task,
        tenant, tenant_setting, tool, trigger, user,
    )
    from app.database import async_session
    from app.services.feishu_identity_maintenance import reconcile_feishu_identity_state

    async with async_session() as db:
        configs = await _load_tenant_feishu_configs(db)
        if not configs:
            logger.warning("No tenant/global Feishu credentials found. Nothing to backfill.")
            return

        user_updates = await _backfill_users(db, configs)
        member_updates = await _backfill_org_members(db, configs)
        maintenance_stats = await reconcile_feishu_identity_state(db)
        await db.commit()

    logger.info("Backfilled users: %s", user_updates)
    logger.info("Backfilled org members: %s", member_updates)
    logger.info("Merged duplicate users: %s", maintenance_stats["merged_users"])
    logger.info("Normalized chat sessions: %s", maintenance_stats["normalized_sessions"])
    logger.info("Feishu identity maintenance complete")


if __name__ == "__main__":
    asyncio.run(main())
