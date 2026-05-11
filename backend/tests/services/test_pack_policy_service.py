from __future__ import annotations

from app.services.pack_policy_service import is_pack_enabled


def test_manifest_inactive_packs_are_disabled_without_tenant_policy():
    assert is_pack_enabled({}, "finance_pack") is False
    assert is_pack_enabled({}, "deep_research_pack") is False
    assert is_pack_enabled({}, "office_pack") is False


def test_explicit_tenant_pack_policy_overrides_manifest_default():
    assert is_pack_enabled({"finance_pack": True}, "finance_pack") is True
    assert is_pack_enabled({"finance_pack": False}, "finance_pack") is False


def test_static_non_manifest_packs_remain_enabled_by_default():
    assert is_pack_enabled({}, "web_pack") is True
