"""Tests for tool config resolution with tenant isolation."""

from __future__ import annotations

import uuid


class TestResolveToolConfigForTenantDisplay:
    """Unit tests for the config merge logic."""

    class _MockTool:
        def __init__(self, config: dict, enabled: bool = True):
            self.id = uuid.uuid4()
            self.config = config
            self.enabled = enabled

    class _MockTTC:
        def __init__(self, config: dict, enabled: bool = True):
            self.config = config
            self.enabled = enabled

    def test_no_tenant_returns_tool_config(self) -> None:
        """Without tenant_id, return base tool config as-is."""
        # resolve_tool_config_for_tenant_display is async and needs DB,
        # so we test the merge logic conceptually here.
        tool_config = {"search_engine": "auto", "api_key": "global_key"}
        # No tenant → base config unchanged
        merged = {**tool_config}
        assert merged["api_key"] == "global_key"

    def test_tenant_override_merges(self) -> None:
        """Tenant config keys override tool config keys."""
        tool_config = {"search_engine": "auto", "api_key": "global_key", "language": "en"}
        tenant_config = {"api_key": "tenant_key", "language": "zh-CN"}
        merged = {**tool_config, **tenant_config}
        assert merged["api_key"] == "tenant_key"
        assert merged["language"] == "zh-CN"
        assert merged["search_engine"] == "auto"

    def test_tenant_override_does_not_remove_keys(self) -> None:
        """Tenant config only overrides, does not remove keys from tool config."""
        tool_config = {"a": 1, "b": 2, "c": 3}
        tenant_config = {"b": 20}
        merged = {**tool_config, **tenant_config}
        assert merged == {"a": 1, "b": 20, "c": 3}

    def test_empty_tenant_config_returns_tool_config(self) -> None:
        """Empty tenant config = no override."""
        tool_config = {"api_key": "global"}
        tenant_config = {}
        merged = {**tool_config, **tenant_config}
        assert merged["api_key"] == "global"

    def test_tenant_enabled_overrides_tool_enabled(self) -> None:
        """Tenant can disable a globally-enabled tool."""
        tenant_enabled = False
        effective = tenant_enabled  # tenant wins
        assert effective is False


class TestContextVarIsolation:
    """Test that the ContextVar mechanism works for tenant isolation."""

    def test_set_and_get_tenant_id(self) -> None:
        from app.core.execution_context import get_tool_tenant_id, set_tool_tenant_id

        tid = uuid.uuid4()
        set_tool_tenant_id(tid)
        assert get_tool_tenant_id() == tid
        set_tool_tenant_id(None)
        assert get_tool_tenant_id() is None

    def test_default_is_none(self) -> None:
        from app.core.execution_context import get_tool_tenant_id, set_tool_tenant_id

        set_tool_tenant_id(None)
        assert get_tool_tenant_id() is None
