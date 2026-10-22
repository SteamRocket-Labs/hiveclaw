"""An actual approval row-lock wait cannot preserve revoked administrator facts."""

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Select, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.audit import ApprovalRequest
from app.models.tenant import Tenant
from app.models.user import User
from tests.services.test_approval_ticket import _seed_approved_ticket

pytestmark = pytest.mark.usefixtures('migrated_pg_url')


@pytest.mark.parametrize('home,change', [('global', None), ('global', 'role'), ('global', 'inactive'),
    ('foreign', None), ('foreign', 'home_inactive'), ('same', 'role'), ('same', 'inactive')])
async def test_ticket_reads_current_approver_after_its_real_row_lock(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, home, change,
):
    from app.config import get_settings
    from app.services.approval_ticket import (
        ApprovalTicketError, build_approval_execution_envelope, consume_approval_ticket, hash_approval_execution_envelope,
    )
    from app.tools.runtime import ToolExecutionContext

    tenant_id, requester_id, agent_id, approval_id, arguments = await _seed_approved_ticket(owner_sessionmaker)
    approver_id = uuid4()
    home_id = None if home == 'global' else tenant_id if home == 'same' else uuid4()
    async with owner_sessionmaker() as db:
        if home == 'foreign':
            db.add(Tenant(id=home_id, name='Administrator home', slug=uuid4().hex))
        db.add(User(id=approver_id, tenant_id=home_id, username=uuid4().hex,
            email=f'{uuid4().hex}@synthetic.invalid', password_hash='synthetic', display_name='Current administrator',
            role='platform_admin', is_active=True))
        await db.flush()
        approval = await db.get(ApprovalRequest, approval_id)
        approval.resolved_by = approver_id
        approval.execution_envelope = build_approval_execution_envelope(context=ToolExecutionContext(agent_id=agent_id,
            user_id=requester_id, tenant_id=str(tenant_id), workspace=Path(get_settings().AGENT_DATA_DIR) / str(agent_id)),
            tool_call_id=f'current-approver-{approval_id}', emit_runtime_hooks=False)
        approval.execution_envelope_hash = hash_approval_execution_envelope(approval.execution_envelope)
        await db.commit()

    waiting, observed = asyncio.Event(), {}

    class ObservedSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            if (isinstance(statement, Select) and statement._for_update_arg is not None and not waiting.is_set()
                and any(column.get('entity') is ApprovalRequest for column in statement.column_descriptions)):
                observed['pid'] = (await super().execute(text('SELECT pg_backend_pid()'))).scalar_one()
                waiting.set()
            return await super().execute(statement, *args, **kwargs)

    factory = async_sessionmaker(app_user_sessionmaker.kw['bind'], class_=ObservedSession, expire_on_commit=False)
    monkeypatch.setattr('app.database.async_session', factory)
    async with owner_sessionmaker() as blocker:
        await blocker.execute(select(ApprovalRequest.id).where(ApprovalRequest.id == approval_id).with_for_update())
        blocker_pid = await blocker.scalar(text('SELECT pg_backend_pid()'))
        consumer = asyncio.create_task(consume_approval_ticket(approval_id=approval_id,
            expected_agent_id=agent_id, expected_user_id=approver_id))
        try:
            await asyncio.wait_for(waiting.wait(), timeout=5)
            async with owner_sessionmaker() as changer:
                # One bounded server-side wait proves the real backend is blocked
                # by this row lock before the independent authority change.
                await changer.execute(text(f"""DO $test$
                    DECLARE deadline timestamptz := clock_timestamp() + interval '3 seconds';
                    BEGIN
                        WHILE NOT {int(blocker_pid)} = ANY(pg_blocking_pids({int(observed['pid'])})) LOOP
                            IF clock_timestamp() > deadline THEN
                                RAISE EXCEPTION 'Ticket consumer did not reach the held row lock';
                            END IF;
                            PERFORM pg_sleep(0.01);
                        END LOOP;
                    END $test$"""))
                assert blocker_pid in await changer.scalar(text('SELECT pg_blocking_pids(:pid)'), {'pid': observed['pid']})
                if change == 'role':
                    (await changer.get(User, approver_id)).role = 'member'
                elif change == 'inactive':
                    (await changer.get(User, approver_id)).is_active = False
                elif change == 'home_inactive':
                    (await changer.get(Tenant, home_id)).is_active = False
                await changer.commit()
            await blocker.rollback()
            if change is not None:
                with pytest.raises(ApprovalTicketError, match='approver is no longer responsible'):
                    await asyncio.wait_for(consumer, timeout=5)
            else:
                ticket = await asyncio.wait_for(consumer, timeout=5)
                assert ticket.approved_by_user_id == approver_id and ticket.requested_by_user_id == requester_id
                assert ticket.arguments == arguments and ticket.tenant_id == tenant_id
        finally:
            await blocker.rollback()
            if not consumer.done():
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)
    async with owner_sessionmaker() as db:
        approval = await db.get(ApprovalRequest, approval_id)
        assert (approval.consumed_at is not None) is (change is None)
        canonical = await db.get(User, approver_id)
        assert canonical.tenant_id == home_id
        assert canonical.role == ('member' if change == 'role' else 'platform_admin')
        assert canonical.is_active is (change != 'inactive')
