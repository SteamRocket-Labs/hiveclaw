from __future__ import annotations

from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _QueuedDB:
    def __init__(self, values):
        self.values = list(values)

    async def execute(self, _stmt):
        return _ScalarResult(self.values.pop(0))


def test_custom_api_capability_definition_is_visible() -> None:
    from app.services.capability_gate import get_all_capabilities

    capabilities = {row["capability"]: row["tools"] for row in get_all_capabilities()}

    assert capabilities["external.api.call"] == ["custom_api__*"]


@pytest.mark.asyncio
async def test_custom_api_tool_maps_to_external_api_capability_without_static_entry() -> None:
    from app.services.capability_gate import check_capability

    result = await check_capability(
        _QueuedDB([uuid4(), None, None]),
        uuid4(),
        uuid4(),
        "custom_api__alphagbm__analyze_stock",
    )

    assert result.allowed is False
    assert result.escalate_to_l3 is True
    assert result.capability == "external.api.call"
    assert result.policy_found is False
