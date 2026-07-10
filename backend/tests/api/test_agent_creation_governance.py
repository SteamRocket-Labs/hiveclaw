from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_direct_agent_create_is_reserved_for_company_admin_control_plane():
    from app.api.agents import create_agent

    with pytest.raises(HTTPException) as exc:
        await create_agent(
            data=SimpleNamespace(),
            current_user=SimpleNamespace(id=uuid4(), role="user", tenant_id=uuid4()),
            db=AsyncMock(),
        )

    assert exc.value.status_code == 403
    assert "HR" in str(exc.value.detail)
