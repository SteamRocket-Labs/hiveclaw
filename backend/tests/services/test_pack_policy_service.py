from __future__ import annotations

from app.services.pack_policy_service import is_pack_enabled


def test_manifest_inactive_packs_are_disabled_without_tenant_policy():
    assert is_pack_enabled({}, "office_pack") is False


def test_explicit_tenant_pack_policy_overrides_manifest_default():
    assert is_pack_enabled({"office_pack": True}, "office_pack") is True
    assert is_pack_enabled({"office_pack": False}, "office_pack") is False


def test_static_non_manifest_packs_remain_enabled_by_default():
    assert is_pack_enabled({}, "web_pack") is True


def test_policy_pack_names_include_manifest_owned_tools_only():
    from app.services.pack_policy_service import policy_pack_names_for_tool

    assert "office_pack" not in policy_pack_names_for_tool("office_document_create")
    assert "office_pack" not in policy_pack_names_for_tool("read_document")
    assert "office_pack" not in policy_pack_names_for_tool("web_search")
    assert "web_pack" not in policy_pack_names_for_tool("web_search")
    assert "web_pack" in policy_pack_names_for_tool("exa_search")
    assert "web_pack" in policy_pack_names_for_tool("tavily_search")
    assert "web_pack" in policy_pack_names_for_tool("anysearch_get_sub_domains")
    assert "web_pack" in policy_pack_names_for_tool("anysearch_search")
    assert "web_pack" in policy_pack_names_for_tool("anysearch_batch_search")
    assert "web_pack" in policy_pack_names_for_tool("anysearch_extract")
    assert "web_pack" in policy_pack_names_for_tool("firecrawl_fetch")
