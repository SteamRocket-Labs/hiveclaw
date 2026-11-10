"""First-task preflight must not accept a colleague-consult tool call."""

import asyncio

import pytest

from app.services.delegation_input_origin import _parent_call
from tests.integration.test_r11c_colleague_lifecycle_postgres import _execute, _setup

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_employee_first_task_parent_call_default_excludes_consult(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    seed, _, _ = await _setup(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        tool="send_message_to_agent")
    await asyncio.wait_for(_execute(seed["run_id"], owner_sessionmaker, worker="r11c-first-task-scope"), 20)
    async with owner_sessionmaker() as db:
        lookup = dict(tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], session_id=seed["session_id"],
            run_id=seed["run_id"], call_id="call-send_message_to_agent")
        assert await _parent_call(db, **lookup) is None
        assert await _parent_call(db, **lookup, tool_name=("delegate_to_agent", "send_message_to_agent")) is not None
