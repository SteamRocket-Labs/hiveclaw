"""Scrub plaintext secrets from shared global tool-config rows (legacy-data fix).

Before per-tenant tool config existed, secret fields (config_schema
``type=password``) were written into the shared ``Tool.config`` row
(``tenant_id IS NULL``). Every tenant — including freshly created ones —
inherits that row as the base config layer, so the first configurer's API
keys leaked platform-wide (the cross-tenant secret leak).

This script moves each non-empty global secret into the platform-admin
tenant's ``TenantToolConfig`` override, then clears it from the global row.
Other tenants that need the capability re-enter their own key.

DRY-RUN by default. ``--apply`` mutates; because clearing the global secret is
irreversible, ``--apply`` also requires ``--confirm``.

Usage:
    python -m app.scripts.scrub_global_tool_secrets               # dry-run report
    python -m app.scripts.scrub_global_tool_secrets --apply --confirm
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.tool import Tool
from app.models.user import User
from app.services.tool_config_service import (
    MASKED_SECRET_SENTINEL,
    _encrypt_secret,
    _secret_field_keys,
    get_or_create_tenant_tool_config,
)


@dataclass(frozen=True)
class ScrubAction:
    """A single global secret field that must be relocated and cleared."""

    tool_id: str
    tool_name: str
    secret_key: str


def plan_global_secret_scrub(
    tool_rows: Iterable[tuple[object, object, dict | None, dict | None]],
) -> list[ScrubAction]:
    """Functional core: decide which global secret fields need moving + clearing.

    ``tool_rows`` yields ``(id, name, config, config_schema)`` for every global
    (``tenant_id IS NULL``) tool. A field qualifies when it is a password-typed
    schema field whose stored value is a non-empty, non-sentinel string.
    """
    actions: list[ScrubAction] = []
    for tool_id, name, config, schema in tool_rows:
        cfg = config or {}
        for key in sorted(_secret_field_keys(schema)):
            value = cfg.get(key)
            if isinstance(value, str) and value and value != MASKED_SECRET_SENTINEL:
                actions.append(ScrubAction(str(tool_id), str(name), key))
    return actions


async def _resolve_platform_tenant_id(db) -> str | None:
    """The platform-admin tenant inherits relocated global secrets."""
    result = await db.execute(
        select(User.tenant_id)
        .where(User.role == "platform_admin", User.tenant_id.isnot(None))
        .order_by(User.created_at.asc())
        .limit(1)
    )
    tid = result.scalar_one_or_none()
    return str(tid) if tid else None


async def scrub(*, apply: bool, confirm: bool) -> int:
    """Report (and optionally apply) the global secret scrub. Returns action count."""
    async with async_session() as db:
        result = await db.execute(
            select(Tool.id, Tool.name, Tool.config, Tool.config_schema).where(Tool.tenant_id.is_(None))
        )
        actions = plan_global_secret_scrub([tuple(row) for row in result.all()])

        if not actions:
            logger.info("[scrub] No plaintext secrets in global tool rows. Nothing to do.")
            return 0

        platform_tid = await _resolve_platform_tenant_id(db)
        logger.info(
            "[scrub] {} global secret field(s) found; platform tenant = {}",
            len(actions),
            platform_tid,
        )
        for action in actions:
            logger.info(
                "[scrub]   tool={} field={} -> relocate to tenant {} + clear global row",
                action.tool_name,
                action.secret_key,
                platform_tid,
            )

        if not apply:
            logger.warning("[scrub] DRY-RUN only. Re-run with --apply --confirm to mutate.")
            return len(actions)
        if not confirm:
            logger.error("[scrub] --apply requires --confirm (irreversible: global secret is cleared). Aborting.")
            return len(actions)
        if not platform_tid:
            logger.error("[scrub] No platform_admin tenant found; cannot relocate secrets safely. Aborting.")
            return len(actions)

        tools_by_id: dict[str, Tool] = {}
        res2 = await db.execute(select(Tool).where(Tool.tenant_id.is_(None)))
        for tool in res2.scalars().all():
            tools_by_id[str(tool.id)] = tool

        moved = 0
        for action in actions:
            tool = tools_by_id.get(action.tool_id)
            if tool is None:
                continue
            value = (tool.config or {}).get(action.secret_key)
            if not value:
                continue
            ttc = await get_or_create_tenant_tool_config(db, uuid.UUID(platform_tid), tool.id)
            new_ttc_config = dict(ttc.config or {})
            # Encrypt on relocation; never clobber an existing tenant key.
            new_ttc_config.setdefault(action.secret_key, _encrypt_secret(value))
            ttc.config = new_ttc_config
            # JSON columns need a fresh dict for SQLAlchemy to detect the change.
            new_global = dict(tool.config or {})
            new_global[action.secret_key] = ""
            tool.config = new_global
            moved += 1

        await db.commit()
        logger.info(
            "[scrub] Applied: {} secret(s) relocated to tenant {} and cleared from global rows.",
            moved,
            platform_tid,
        )
        return moved


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrub plaintext secrets from shared global tool-config rows.")
    parser.add_argument("--apply", action="store_true", help="Mutate the database (default: dry-run).")
    parser.add_argument("--confirm", action="store_true", help="Required with --apply (irreversible).")
    args = parser.parse_args()
    asyncio.run(scrub(apply=args.apply, confirm=args.confirm))


if __name__ == "__main__":
    main()
