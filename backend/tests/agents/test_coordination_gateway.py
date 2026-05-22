"""Tests for the in-process CoordinationGateway adapter."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.agents.coordination import CoordinationRuntime
from app.agents.coordination_gateway import (
    CoordinationGateway,
    InProcessCoordinationGateway,
    default_in_process_gateway,
)


@pytest.fixture
def runtime() -> CoordinationRuntime:
    return CoordinationRuntime()


@pytest.fixture
def gateway(runtime: CoordinationRuntime) -> InProcessCoordinationGateway:
    return InProcessCoordinationGateway(runtime)


class TestProtocolSatisfaction:
    def test_in_process_gateway_satisfies_protocol(self, gateway: InProcessCoordinationGateway) -> None:
        assert isinstance(gateway, CoordinationGateway)


class TestLease:
    @pytest.mark.asyncio
    async def test_acquire_lease_wraps_runtime(self, gateway: InProcessCoordinationGateway) -> None:
        result = await gateway.acquire_lease(task_key="t-1", agent_id="agent_a", ttl_seconds=60)
        assert result.acquired is True
        assert result.lease is not None
        again = await gateway.acquire_lease(task_key="t-1", agent_id="agent_b", ttl_seconds=60)
        assert again.acquired is False
        assert again.existing_lease_id == result.lease.id


class TestSignals:
    @pytest.mark.asyncio
    async def test_send_and_read_signals(self, gateway: InProcessCoordinationGateway) -> None:
        sent = await gateway.send_signal(
            from_agent_id="a",
            to_agent_id="b",
            content="hello",
            signal_type="note",
            thread_id="t1",
        )
        signals = await gateway.read_signals("b", thread_id="t1")
        assert [s.id for s in signals] == [sent.id]


class TestCheckpoint:
    @pytest.mark.asyncio
    async def test_create_and_get_checkpoint(self, gateway: InProcessCoordinationGateway) -> None:
        deadline = datetime(2026, 5, 22, 18, 0, tzinfo=timezone.utc)
        checkpoint = await gateway.create_checkpoint(
            action="send_feishu_message",
            approver_id="alice",
            escalation_chain=["company_admin"],
            deadline_at=deadline,
            metadata={"tool_name": "send_feishu_message"},
        )
        roundtrip = await gateway.get_checkpoint(checkpoint.id)
        assert roundtrip is not None
        assert roundtrip.id == checkpoint.id

    @pytest.mark.asyncio
    async def test_get_unknown_returns_none(self, gateway: InProcessCoordinationGateway) -> None:
        assert await gateway.get_checkpoint("00000000-0000-0000-0000-000000000000") is None

    @pytest.mark.asyncio
    async def test_escalate_advances_approver(self, gateway: InProcessCoordinationGateway) -> None:
        deadline = datetime.now(timezone.utc) - timedelta(seconds=30)
        checkpoint = await gateway.create_checkpoint(
            action="x",
            approver_id="alice",
            escalation_chain=["company_admin"],
            deadline_at=deadline,
            metadata={},
        )
        escalated = await gateway.escalate_expired_checkpoints()
        assert checkpoint.id in escalated


def test_default_gateway_returns_in_process_implementation() -> None:
    assert isinstance(default_in_process_gateway(), InProcessCoordinationGateway)
