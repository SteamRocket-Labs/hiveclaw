"""Terminal claims cannot outlive their deadline during a real row-lock wait."""

import asyncio
import json

import pytest
from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
from app.services.runtime_terminal_boundary_outbox import (
    RuntimeTerminalBoundaryOutboxService,
    StaleTerminalBoundaryClaim,
)
from tests.services.test_runtime_terminal_boundary_outbox import _binding, _enqueue, _seed_terminal_task

pytestmark = pytest.mark.usefixtures('migrated_pg_url')


@pytest.mark.parametrize('task_type', ['business_task', 'delegation'])
async def test_terminal_claim_keeps_large_task_bodies_out_of_the_locked_queue_sort(
    owner_sessionmaker, tmp_path, task_type,
):
    from types import SimpleNamespace
    import secrets
    import uuid

    from app.models.runtime_task import RuntimeTask

    seeded = await _seed_terminal_task(owner_sessionmaker, tenant_name='Large terminal claim', task_type=task_type)
    identities = [seeded.task_id, *(uuid.uuid4() for _ in range(131))]
    sibling_identities = [uuid.uuid4() for _ in range(128)]
    padding = secrets.token_hex(128 * 1024)
    async with owner_sessionmaker() as db:
        original = await db.get(RuntimeTask, seeded.task_id)
        original.metadata_json = {'synthetic_padding': padding}
        for identity in [*identities[1:], *sibling_identities]:
            db.add(RuntimeTask(id=identity, tenant_id=seeded.tenant_id,
                task_type=task_type if identity in identities else 'web_chat_turn', status='completed',
                parent_agent_id=seeded.agent_id, parent_session_id=str(seeded.session_id),
                root_user_id=seeded.user_id, root_session_id=str(seeded.session_id),
                prompt='Synthetic queue input', result_summary='Synthetic queue result',
                metadata_json={'synthetic_padding': padding}))
        for _ in range(8192):
            db.add(RuntimeTask(id=uuid.uuid4(), tenant_id=seeded.tenant_id, task_type=task_type,
                status='completed', parent_agent_id=seeded.agent_id,
                parent_session_id=str(seeded.session_id), root_user_id=seeded.user_id,
                root_session_id=str(seeded.session_id), prompt='Synthetic unqueued historical task'))
        await db.commit()
    for identity in [*sibling_identities, *identities]:
        binding = _binding()
        binding['source_refs'] = [{'event_id': str(uuid.uuid4()), 'sequence': index,
            'sha256': secrets.token_hex(32)} for index in range(128)]
        await _enqueue(owner_sessionmaker, SimpleNamespace(**{**vars(seeded), 'task_id': identity}),
            event_kind='turn_stop', binding=binding)

    class LimitedSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            if not self.info.get('limited_queue_role'):
                self.info['limited_queue_role'] = True
                for setting in ["SET LOCAL temp_file_limit='128kB'", "SET LOCAL work_mem='64kB'",
                    'SET LOCAL enable_nestloop=off', 'SET LOCAL enable_mergejoin=off', 'SET LOCAL ROLE rls_app_user']:
                    await super().execute(text(setting))
                role = (await super().execute(text('SELECT current_user, rolsuper, rolbypassrls '
                    'FROM pg_roles WHERE rolname=current_user'))).one()
                assert role == ('rls_app_user', False, False)
            return await super().execute(statement, *args, **kwargs)

    factory = async_sessionmaker(owner_sessionmaker.kw['bind'], class_=LimitedSession, expire_on_commit=False)
    service = RuntimeTerminalBoundaryOutboxService(session_factory=factory)
    claimed = await service.claim_batch(tenant_id=seeded.tenant_id, worker_id='limited-terminal-queue',
        task_types=(task_type,), limit=100)
    assert len(claimed) == 1 and claimed[0].runtime_task_id in identities
    assert claimed[0].tenant_id == seeded.tenant_id
    async with owner_sessionmaker() as blocker:
        await blocker.scalar(select(RuntimeTask).where(RuntimeTask.id == identities[1]).with_for_update())
        next_claim = await service.claim_batch(tenant_id=seeded.tenant_id, worker_id='next-terminal-queue',
            task_types=(task_type,), limit=100)
        assert len(next_claim) == 1 and next_claim[0].id != claimed[0].id
        assert next_claim[0].runtime_task_id == identities[1]
        await blocker.execute(select(RuntimeTerminalBoundaryOutbox.id).where(
            RuntimeTerminalBoundaryOutbox.tenant_id == seeded.tenant_id,
            RuntimeTerminalBoundaryOutbox.runtime_task_id.in_(identities[2:102])).with_for_update())
        after_locked_page = await service.claim_batch(tenant_id=seeded.tenant_id,
            worker_id='after-locked-terminal-page', task_types=(task_type,), limit=100)
        assert len(after_locked_page) == 1 and after_locked_page[0].runtime_task_id == identities[102]
    async with owner_sessionmaker() as db:
        pending_siblings = await db.scalar(select(func.count()).select_from(RuntimeTerminalBoundaryOutbox).where(
            RuntimeTerminalBoundaryOutbox.tenant_id == seeded.tenant_id,
            RuntimeTerminalBoundaryOutbox.runtime_task_id.in_(sibling_identities),
            RuntimeTerminalBoundaryOutbox.status == 'pending'))
        assert pending_siblings == len(sibling_identities)
    (tmp_path / 'bounded-terminal-queue.json').write_text(json.dumps({
        'task_type': task_type, 'queued': len(identities), 'sibling_lane_backlog': len(sibling_identities),
        'task_padding_bytes': len(padding), 'unqueued_historical_tasks': 8192,
        'temp_file_limit_kb': 128, 'work_mem_kb': 64, 'claimed': len(claimed), 'next_claim': len(next_claim),
        'role': 'rls_app_user', 'superuser': False, 'bypassrls': False}, indent=2))


async def test_terminal_claim_lease_starts_after_a_long_identity_scan(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from datetime import UTC, datetime, timedelta
    from app.services import runtime_terminal_boundary_outbox as boundary

    seeded = await _seed_terminal_task(owner_sessionmaker, tenant_name='Long terminal identity scan')
    await _enqueue(owner_sessionmaker, seeded, event_kind='turn_stop', binding=_binding())
    clock = {'now': datetime.now(UTC), 'advanced': False}

    class ScanClock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = clock['now']
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    class DelayedScanSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            result = await super().execute(statement, *args, **kwargs)
            if isinstance(statement, Select) and statement._for_update_arg is None and not clock['advanced']:
                clock['advanced'] = True
                clock['now'] += timedelta(seconds=120)
            return result

    monkeypatch.setattr(boundary, 'datetime', ScanClock)
    factory = async_sessionmaker(app_user_sessionmaker.kw['bind'], class_=DelayedScanSession,
        expire_on_commit=False)
    service = RuntimeTerminalBoundaryOutboxService(session_factory=factory, lease_seconds=60)
    [item] = await service.claim_batch(tenant_id=seeded.tenant_id, worker_id='long-scan-claim')
    assert clock['advanced']
    async with owner_sessionmaker() as db:
        row = await db.get(RuntimeTerminalBoundaryOutbox, item.id)
        assert row.lease_expires_at > clock['now']


@pytest.mark.parametrize('operation', ['renew', 'process'])
@pytest.mark.parametrize('expires_during_wait', [False, True])
async def test_terminal_claim_checks_current_deadline_after_row_lock(
    owner_sessionmaker, app_user_sessionmaker, tmp_path, operation, expires_during_wait,
):
    seeded = await _seed_terminal_task(owner_sessionmaker, tenant_name='Current terminal claim')
    await _enqueue(owner_sessionmaker, seeded, event_kind='turn_stop', binding=_binding())
    waiting, observed = asyncio.Event(), {}

    class ObservedSession(AsyncSession):
        async def scalar(self, statement, *args, **kwargs):
            if (isinstance(statement, Select) and statement._for_update_arg is not None
                and not waiting.is_set()
                and any(column.get('entity') is RuntimeTerminalBoundaryOutbox
                    for column in statement.column_descriptions)):
                observed['pid'] = await super().scalar(text('SELECT pg_backend_pid()'))
                waiting.set()
            return await super().scalar(statement, *args, **kwargs)

    factory = async_sessionmaker(app_user_sessionmaker.kw['bind'], class_=ObservedSession, expire_on_commit=False)
    service = RuntimeTerminalBoundaryOutboxService(session_factory=factory, lease_seconds=60)
    [item] = await service.claim_batch(tenant_id=seeded.tenant_id, worker_id='terminal-current-worker')
    async with owner_sessionmaker() as db:
        row = await db.get(RuntimeTerminalBoundaryOutbox, item.id)
        deadline = await db.scalar(text("SELECT clock_timestamp() + :seconds * interval '1 second'"),
            {'seconds': 2 if expires_during_wait else 60})
        row.lease_expires_at = deadline
        await db.commit()
    validator_calls, processor_calls = [], []

    async def exact(_db, claimed):
        validator_calls.append(claimed.id)
        return dict(claimed.binding)

    async def process(claimed):
        processor_calls.append(claimed.id)
        return {'boundary_id': str(claimed.id)}

    async def invoke():
        try:
            if operation == 'renew':
                return await service.renew_terminal_boundary_claim(item=item, worker_id='terminal-current-worker')
            return await service.process_terminal_boundary(item=item, worker_id='terminal-current-worker',
                canonical_validator=exact, process_callback=process)
        except StaleTerminalBoundaryClaim:
            return 'stale'

    async with owner_sessionmaker() as blocker:
        await blocker.scalar(select(RuntimeTerminalBoundaryOutbox).where(
            RuntimeTerminalBoundaryOutbox.id == item.id).with_for_update())
        blocker_pid = await blocker.scalar(text('SELECT pg_backend_pid()'))
        request = asyncio.create_task(invoke())
        try:
            await asyncio.wait_for(waiting.wait(), timeout=10)
            async with owner_sessionmaker() as observer:
                await observer.execute(text(f"""DO $test$
                    DECLARE deadline timestamptz := clock_timestamp() + interval '3 seconds';
                    BEGIN
                        WHILE NOT {int(blocker_pid)} = ANY(pg_blocking_pids({int(observed['pid'])})) LOOP
                            IF clock_timestamp() > deadline THEN RAISE EXCEPTION 'Claim did not reach row lock'; END IF;
                            PERFORM pg_sleep(0.01);
                        END LOOP;
                    END $test$"""))
                assert blocker_pid in await observer.scalar(text('SELECT pg_blocking_pids(:pid)'),
                    {'pid': observed['pid']})
                if expires_during_wait:
                    await observer.execute(text('SELECT pg_sleep(GREATEST(EXTRACT(EPOCH FROM '
                        '(CAST(:deadline AS timestamptz) - clock_timestamp())), 0) + 0.05)'), {'deadline': deadline})
                assert bool(await observer.scalar(text('SELECT clock_timestamp() >= :deadline'),
                    {'deadline': deadline})) is expires_during_wait
            await blocker.commit()
            result = await asyncio.wait_for(request, timeout=10)
        finally:
            await blocker.rollback()
            if not request.done():
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
    async with owner_sessionmaker() as db:
        row = await db.get(RuntimeTerminalBoundaryOutbox, item.id)
        observed.update(operation=operation, expires_during_wait=expires_during_wait, result=result,
            status=row.status, deadline_preserved=row.lease_expires_at == deadline,
            validator_calls=len(validator_calls), processor_calls=len(processor_calls))
    (tmp_path / 'terminal-current-frame.json').write_text(json.dumps(observed, indent=2))
    if expires_during_wait:
        assert result == (False if operation == 'renew' else 'stale'), observed
        assert row.status == 'processing' and row.lease_expires_at == deadline, observed
        assert validator_calls == processor_calls == [], observed
    else:
        assert result is True, observed
        assert row.status == ('processing' if operation == 'renew' else 'delivered'), observed
        assert len(validator_calls) == len(processor_calls) == (0 if operation == 'renew' else 1), observed
