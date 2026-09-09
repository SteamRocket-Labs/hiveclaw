from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.services.test_session_permission_runtime import (
    _authority,
    _install_successful_executor,
    _seed_waiting_batch,
)


@pytest.mark.usefixtures("migrated_pg_url")
@pytest.mark.parametrize("counterexample", [None, "wrong_tool", "wrong_user", "wrong_status"])
async def test_native_decline_binds_receipt_without_prose_and_only_replays_same_run(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, counterexample
):
    from app.database import tenant_scoped_session
    from app.services import plan_mode_core
    from app.services.session_permission_runtime import resolve_session_tool_permission
    from app.services.web_chat_run_orchestrator import _bind_trusted_decline

    seed = await _seed_waiting_batch(
        owner_sessionmaker,
        tool_name="read_file" if counterexample == "wrong_tool" else "request_plan_mode",
    )
    _install_successful_executor(
        monkeypatch,
        result=json.dumps(
            {
                "status": "error" if counterexample == "wrong_status" else "plan_mode_entry_requested",
                "requested_by_user_id": str(uuid4() if counterexample == "wrong_user" else seed.user_id),
                "reason": "Confirm a bounded scheduled task.",
            }
        ),
    )
    async with owner_sessionmaker() as db:
        _, authority = await _authority(db, seed)
        await resolve_session_tool_permission(
            db, authority=authority, permission_request_id=seed.permission_ids[0], decision="allow_once"
        )
        await db.commit()

    @asynccontextmanager
    async def scoped(tenant_id):
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            yield db

    warnings = []
    state = SimpleNamespace(
        internal_runtime_context_turn=False,
        metadata={},
        prompt="Continue without entering Plan Mode.",
        history_messages=[],
        actor_user_id=seed.user_id,
        agent=SimpleNamespace(id=seed.agent_id, tenant_id=seed.tenant_id),
        session_id=str(seed.session_id),
        run_uuid=uuid4(),
        ports=SimpleNamespace(
            runtime=SimpleNamespace(
                plan_mode_core=plan_mode_core,
                tenant_scoped_session=scoped,
                logger=SimpleNamespace(warning=lambda *args: warnings.append(args)),
            )
        ),
    )
    first = await _bind_trusted_decline(state)
    assert not warnings
    if counterexample:
        assert first is None
        return
    assert first and first["recommendation_id"]
    assert await _bind_trusted_decline(state) == first
    state.run_uuid = uuid4()
    assert await _bind_trusted_decline(state) is None
    state.internal_runtime_context_turn = True
    assert await _bind_trusted_decline(state) is None


def test_ui_decline_is_not_a_first_turn_authorization():
    from app.services.plan_mode_core import classify_plan_mode_entry, trusted_decline_metadata

    for content in ("Continue without entering Plan Mode.", "继续处理，不需要进入计划模式。"):
        assert classify_plan_mode_entry(content).mode == "declined"
        assert trusted_decline_metadata(content=content, messages=[]) is None
        assert classify_plan_mode_entry(content, explicit=True).mode == "explicit"
