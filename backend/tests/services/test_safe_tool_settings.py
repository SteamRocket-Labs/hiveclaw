"""One setting classification governs display, handover and masked saves."""

import pytest

from app.services.tool_config_service import (
    MASKED_SECRET_SENTINEL,
    clear_agent_tool_config_secrets,
    mask_agent_tool_config_secrets,
    merge_agent_tool_config_secrets,
)


@pytest.mark.parametrize("field,value,safe", [
    ({"key": "timeout", "type": "number"}, 0, True),
    ({"key": "enabled", "type": "boolean"}, False, True),
    # Less code 2: safe fixture fields use the current platform's select schema.
    ({"key": "region", "type": "select"}, "local", True),
    ({"key": "endpoint", "type": "select"}, "https://example.test/api?region=local", True),
    ({"key": "endpoint", "type": "text"}, "https://example.test/api?api_key=fake", False),
    ({"key": "endpoint", "type": "text"}, "https://user:fake@example.test/api", False),
    ({"key": "endpoint", "type": "text"}, "https://example.test/api?%74oken=fake", False),
    ({"key": "custom", "type": "password"}, "fake", False),
    ({"key": "custom", "format": "password"}, "fake", False),
    ({"key": "custom", "writeOnly": True}, "fake", False),
    ({"key": "api_key", "type": "text"}, "fake", False),
    ({"key": "headers", "type": "text"}, "fake", False),
    ({"key": "auth", "type": "text"}, "fake", False),
    ({"key": "env", "type": "text"}, "fake", False),
    ({"key": "nested", "type": "text"}, {"region": "local", "Authorization": "fake"}, False),
    ({"key": "nested", "type": "text"}, ["fake"], False),
])
@pytest.mark.parametrize("platform_tool", [True, False])
def test_the_same_rule_masks_clears_and_preserves_a_sentinel(field, value, safe, platform_tool):
    key, schema = field["key"], {"fields": [field]}
    stored = {key: value, "vendor_pin": "fake"}
    visible = safe and platform_tool
    masked = mask_agent_tool_config_secrets(stored, schema, platform_tool=platform_tool)
    assert masked[key] == (value if visible or value in (None, "", {}, []) else MASKED_SECRET_SENTINEL)
    assert masked["vendor_pin"] == MASKED_SECRET_SENTINEL
    cleared = clear_agent_tool_config_secrets(stored, schema, platform_tool=platform_tool)
    # S1: handover keeps declared safe scalars even on a tenant-defined tool.
    assert cleared == {key: value if safe else "", "vendor_pin": ""}
    assert merge_agent_tool_config_secrets(masked, cleared, schema, platform_tool=platform_tool) == cleared
    assert merge_agent_tool_config_secrets({key: "new-input"}, cleared, schema, platform_tool=platform_tool)[key] == "new-input"
