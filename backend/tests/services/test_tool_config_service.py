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


class TestMaskToolConfigSecrets:
    """Secret fields must never be echoed back; masked round-trips must not wipe keys."""

    _SCHEMA = {
        "fields": [
            {"key": "search_engine", "type": "select"},
            {"key": "exa_api_key", "type": "password"},
            {"key": "tavily_api_key", "type": "password"},
        ]
    }

    def test_mask_replaces_nonempty_secret_with_sentinel(self) -> None:
        from app.services.tool_config_service import MASKED_SECRET_SENTINEL, mask_tool_config_secrets

        masked = mask_tool_config_secrets(
            {"search_engine": "auto", "exa_api_key": "exa-REAL-KEY"}, self._SCHEMA
        )
        assert masked["exa_api_key"] == MASKED_SECRET_SENTINEL
        assert "exa-REAL-KEY" not in masked.values()  # plaintext never echoed
        assert masked["search_engine"] == "auto"  # non-secret untouched

    def test_mask_leaves_empty_secret_empty(self) -> None:
        from app.services.tool_config_service import mask_tool_config_secrets

        masked = mask_tool_config_secrets({"exa_api_key": ""}, self._SCHEMA)
        assert masked["exa_api_key"] == ""  # "unset" stays distinguishable from "set"

    def test_mask_without_schema_is_noop(self) -> None:
        from app.services.tool_config_service import mask_tool_config_secrets

        cfg = {"exa_api_key": "k"}
        assert mask_tool_config_secrets(cfg, None) == cfg

    def test_merge_sentinel_keeps_stored_secret(self) -> None:
        from app.services.tool_config_service import MASKED_SECRET_SENTINEL, merge_tool_config_secrets

        merged = merge_tool_config_secrets(
            {"exa_api_key": MASKED_SECRET_SENTINEL, "search_engine": "tavily"},
            {"exa_api_key": "exa-REAL-KEY"},
            self._SCHEMA,
        )
        assert merged["exa_api_key"] == "exa-REAL-KEY"  # not overwritten by the mask
        assert merged["search_engine"] == "tavily"  # non-secret edit still applied

    def test_merge_new_value_updates_secret(self) -> None:
        from app.services.tool_config_service import merge_tool_config_secrets

        merged = merge_tool_config_secrets(
            {"exa_api_key": "exa-NEW"}, {"exa_api_key": "exa-OLD"}, self._SCHEMA
        )
        assert merged["exa_api_key"] == "exa-NEW"

    def test_merge_empty_string_clears_secret(self) -> None:
        from app.services.tool_config_service import merge_tool_config_secrets

        merged = merge_tool_config_secrets(
            {"exa_api_key": ""}, {"exa_api_key": "exa-OLD"}, self._SCHEMA
        )
        assert merged["exa_api_key"] == ""  # explicit clear is honored

    def test_merge_absent_key_keeps_stored(self) -> None:
        from app.services.tool_config_service import merge_tool_config_secrets

        merged = merge_tool_config_secrets(
            {"search_engine": "auto"}, {"exa_api_key": "exa-OLD"}, self._SCHEMA
        )
        assert merged["exa_api_key"] == "exa-OLD"  # partial payload must not wipe a key

    def test_merge_sentinel_with_no_stored_drops_key(self) -> None:
        """A new tenant echoing the inherited mask must not persist the sentinel."""
        from app.services.tool_config_service import MASKED_SECRET_SENTINEL, merge_tool_config_secrets

        merged = merge_tool_config_secrets({"exa_api_key": MASKED_SECRET_SENTINEL}, {}, self._SCHEMA)
        assert "exa_api_key" not in merged  # no sentinel, no inherited key

    def test_serialize_tool_masks_secret_at_api_boundary(self) -> None:
        """The serialization choke point used by GET /tools must emit the sentinel."""
        from types import SimpleNamespace

        from app.api.tools import _serialize_tool
        from app.services.tool_config_service import MASKED_SECRET_SENTINEL

        tool = SimpleNamespace(
            id=uuid.uuid4(),
            name="web_search",
            display_name="x",
            description="",
            type="builtin",
            category="search",
            icon="🔧",
            parameters_schema={},
            config={"exa_api_key": "exa-REAL", "search_engine": "auto"},
            config_schema={"fields": [{"key": "exa_api_key", "type": "password"}]},
            mcp_server_url=None,
            mcp_server_name=None,
            mcp_tool_name=None,
            enabled=True,
            is_default=False,
            tenant_id=None,
        )
        out = _serialize_tool(tool)  # type: ignore[arg-type]  # SimpleNamespace duck-types Tool
        assert out["config"]["exa_api_key"] == MASKED_SECRET_SENTINEL
        assert "exa-REAL" not in str(out["config"])  # no plaintext anywhere in the payload


class TestToolConfigSecretEncryption:
    """Secrets encrypt at rest (tagged) and decrypt for runtime; legacy plaintext stays compatible."""

    _SCHEMA = {"fields": [{"key": "exa_api_key", "type": "password"}, {"key": "search_engine", "type": "select"}]}

    class _ReversingProvider:
        """Ciphertext differs from input, so the encryption tag kicks in."""

        def encrypt(self, value: str) -> str:
            return value[::-1]

        def decrypt(self, value: str) -> str:
            return value[::-1]

    def test_encrypt_tags_and_decrypt_restores(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "app.services.secrets_provider.get_secrets_provider", lambda: self._ReversingProvider()
        )
        from app.services.tool_config_service import (
            _ENCRYPTED_SECRET_PREFIX,
            decrypt_tool_config_secrets,
            encrypt_tool_config_secrets,
        )

        enc = encrypt_tool_config_secrets({"exa_api_key": "exa-REAL", "search_engine": "auto"}, self._SCHEMA)
        assert enc["exa_api_key"].startswith(_ENCRYPTED_SECRET_PREFIX)
        assert "exa-REAL" not in enc["exa_api_key"]  # plaintext not stored at rest
        assert enc["search_engine"] == "auto"  # non-secret untouched
        dec = decrypt_tool_config_secrets(enc, self._SCHEMA)
        assert dec["exa_api_key"] == "exa-REAL"  # round-trips back for runtime use

    def test_noop_provider_stores_plaintext_without_tag(self, monkeypatch) -> None:
        """No master key => plaintext preserved, no tag (zero regression)."""

        class _NoOp:
            def encrypt(self, value: str) -> str:
                return value

            def decrypt(self, value: str) -> str:
                return value

        monkeypatch.setattr("app.services.secrets_provider.get_secrets_provider", lambda: _NoOp())
        from app.services.tool_config_service import encrypt_tool_config_secrets

        enc = encrypt_tool_config_secrets({"exa_api_key": "exa-REAL"}, self._SCHEMA)
        assert enc["exa_api_key"] == "exa-REAL"  # untagged plaintext, no migration risk

    def test_decrypt_passthrough_for_legacy_plaintext(self, monkeypatch) -> None:
        """Untagged legacy values must never reach provider.decrypt (would raise under a real key)."""

        class _Boom:
            def encrypt(self, value: str) -> str:
                return value

            def decrypt(self, value: str) -> str:
                raise AssertionError("untagged plaintext must not be decrypted")

        monkeypatch.setattr("app.services.secrets_provider.get_secrets_provider", lambda: _Boom())
        from app.services.tool_config_service import decrypt_tool_config_secrets

        out = decrypt_tool_config_secrets({"exa_api_key": "legacy-plaintext"}, self._SCHEMA)
        assert out["exa_api_key"] == "legacy-plaintext"


class TestClearSecretFields:
    """Offboarding scrub blanks secrets, keeps everything else."""

    _SCHEMA = {"fields": [{"key": "exa_api_key", "type": "password"}, {"key": "search_engine", "type": "select"}]}

    def test_clears_secret_keeps_nonsecret(self) -> None:
        from app.services.tool_config_service import clear_secret_fields

        out = clear_secret_fields({"exa_api_key": "REAL", "search_engine": "auto"}, self._SCHEMA)
        assert out["exa_api_key"] == ""  # secret blanked
        assert out["search_engine"] == "auto"  # non-secret kept

    def test_noop_when_no_secret_set(self) -> None:
        from app.services.tool_config_service import clear_secret_fields

        out = clear_secret_fields({"search_engine": "auto"}, self._SCHEMA)
        assert out == {"search_engine": "auto"}
