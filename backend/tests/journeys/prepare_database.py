"""Owner-role data bootstrap required by a fresh atomic-journey database."""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.database import async_session
from app.models.tenant import Tenant


async def prepare_database() -> None:
    async with async_session() as db:
        existing = await db.execute(select(Tenant).where(Tenant.slug == "default"))
        if existing.scalar_one_or_none() is None:
            db.add(Tenant(name="Default", slug="default", im_provider="web_only"))
            await db.commit()


def main() -> None:
    asyncio.run(prepare_database())


if __name__ == "__main__":
    main()
