"""Verify per-statement attribution across SQLAlchemy's async bridge."""

import asyncio
import re
from time import perf_counter
from uuid import uuid4

from app.core.request_timing import provider_request_timing, run_timing_scope, timed_phase
from app.database import pin_rls_tenant_context, tenant_scoped_session


async def test_concurrent_phases_attribute_only_their_own_statements(app_user_sessionmaker):
    tenant = uuid4()
    memory_entered = asyncio.Event()
    tools_entered = asyncio.Event()
    memory_finished = asyncio.Event()

    async def open_scoped_session():
        async with tenant_scoped_session(tenant, session_factory=app_user_sessionmaker):
            pass

    @timed_phase("memory_context")
    async def memory():
        memory_entered.set()
        await tools_entered.wait()
        await open_scoped_session()
        await open_scoped_session()
        memory_finished.set()

    @timed_phase("tool_collection")
    async def tools():
        await memory_entered.wait()
        tools_entered.set()
        await memory_finished.wait()
        async with app_user_sessionmaker() as session:
            await session.connection()
            for _ in range(3):
                await pin_rls_tenant_context(session, tenant)

    with run_timing_scope(uuid4()) as timing:
        await open_scoped_session()
        await asyncio.gather(memory(), tools())
        with provider_request_timing():
            pass
        await open_scoped_session()
        with provider_request_timing():
            pass
        await open_scoped_session()
        with provider_request_timing():
            pass
        await open_scoped_session()
        await open_scoped_session()

    sites = timing.phase_sites_summary(timing.summary(perf_counter()))
    assert set(sites) == {
        "memory_context", "tool_collection", "pre_model_other", "between_rounds_other", "post_model_other",
    }
    for phase, count in (
        ("memory_context", 2), ("tool_collection", 3), ("pre_model_other", 1),
        ("between_rounds_other", 2), ("post_model_other", 2),
    ):
        assert sites[phase]["other_n"] == sites[phase]["other_db_ms"] == 0
        (signature, stats), = sites[phase]["sites"].items()
        assert stats["n"] == count
        assert stats["db_ms"] > 0
        if phase == "tool_collection":
            assert re.fullmatch(
                r"app\.database:_apply_rls_pin:\d+ < app\.database:pin_rls_tenant_context:\d+", signature,
            )
        else:
            assert re.fullmatch(
                r"app\.database:_apply_rls_tenant_for_transaction:\d+ < app\.database:_apply_rls_pin:\d+"
                r" < app\.database:pin_rls_tenant_context:\d+", signature,
            )
    assert timing.statements == 10
