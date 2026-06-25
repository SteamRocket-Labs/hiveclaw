"""ToolSpecV1 is derived from the live @tool ToolMeta registry (U4).

These tests exercise the real derivation path: they read the decorator-sourced
ToolMeta for actually-registered tools and assert the projected ToolSpecV1
fields. They fail if the derivation in app.tools.registry.tool_spec_v1 is
reverted (e.g. capability/defer/always/result_budget no longer mapped).
"""

from __future__ import annotations

from app.runtime.ccplus_contracts import ToolSpecV1
from app.tools.collector import collect_tools
from app.tools.decorator import get_all_registered_tools
from app.tools.registry import tool_spec_v1, tool_specs_v1
from app.tools.runtime_tool_groups import RUNTIME_TOOL_GROUPS


def test_unknown_tool_name_has_no_spec() -> None:
    assert tool_spec_v1("definitely_not_a_registered_tool") is None


def test_deferred_pack_tool_spec_carries_capability_and_flags() -> None:
    # exa_search is a real registered tool inside the deferred web_pack runtime
    # group: read-only, parallel-safe, never evicted (max_result_chars=0).
    spec = tool_spec_v1("exa_search")
    assert isinstance(spec, ToolSpecV1)
    assert spec.name == "exa_search"
    # capability is the deferred runtime tool group bundle, not the category.
    assert spec.capability == "web_pack"
    assert spec.read_only is True
    assert spec.concurrency_safe is True  # mapped from ToolMeta.parallel_safe
    assert spec.defer_loading is True  # member of a deferred runtime tool group
    assert spec.always_load is False  # deferred tools are not CORE
    assert spec.result_budget == 0  # ToolMeta.max_result_chars passthrough
    # input_schema is the real parameter schema, not an empty default.
    assert spec.input_schema.get("type") == "object"
    assert "query" in spec.input_schema.get("properties", {})


def test_core_tool_spec_is_always_load_and_not_deferred() -> None:
    # read_file is a CORE filesystem tool: belongs to no deferred runtime group.
    spec = tool_spec_v1("read_file")
    assert isinstance(spec, ToolSpecV1)
    assert spec.capability == "File System"  # infer_category fallback for CORE
    assert spec.defer_loading is False
    assert spec.always_load is True
    assert spec.read_only is True


def test_spec_resolves_alias_to_canonical_with_aliases_listed() -> None:
    # bing_search is an alias of web_search (a CORE tool with an alias tuple).
    spec = tool_spec_v1("bing_search")
    assert isinstance(spec, ToolSpecV1)
    assert spec.name == "web_search"  # alias resolves to canonical name
    assert "bing_search" in spec.aliases


def test_spec_matches_live_toolmeta_destructive_and_budget() -> None:
    # Cross-check the derivation against the raw ToolMeta for the same tool so
    # the test is pinned to the live decorator metadata, not a hardcoded value.
    collect_tools()  # ensure handler modules imported -> registry populated
    registered = get_all_registered_tools()
    meta, _fn = registered["read_file"]
    spec = tool_spec_v1("read_file")
    assert spec is not None
    assert spec.destructive == bool(meta.destructive)
    assert spec.read_only == bool(meta.read_only)
    assert spec.concurrency_safe == bool(meta.parallel_safe)
    assert spec.result_budget == meta.max_result_chars
    assert spec.aliases == tuple(meta.aliases)


def test_tool_specs_v1_covers_every_deferred_pack_member() -> None:
    # Every tool listed in the deferred runtime tool groups that is actually
    # registered must derive a deferred ToolSpecV1.
    pack_tools = {name for group in RUNTIME_TOOL_GROUPS for name in group.tools}
    specs = {s.name: s for s in tool_specs_v1(sorted(pack_tools))}
    assert specs, "expected at least one deferred-pack ToolSpecV1"
    for spec in specs.values():
        assert spec.defer_loading is True
        assert spec.always_load is False
        assert spec.capability  # capability bundle is always populated
