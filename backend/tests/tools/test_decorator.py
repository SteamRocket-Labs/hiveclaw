"""Tests for the @tool decorator and ToolMeta."""

from app.tools.decorator import ToolMeta, clear_registry, get_all_registered_tools, tool


def setup_function():
    clear_registry()


def test_tool_decorator_registers_handler():
    meta = ToolMeta(
        name="test_tool",
        description="A test tool",
        parameters={"type": "object", "properties": {}},
        category="test",
        display_name="Test Tool",
    )

    @tool(meta)
    async def test_tool(arguments: dict) -> str:
        return "ok"

    registry = get_all_registered_tools()
    assert "test_tool" in registry
    assert registry["test_tool"][0] is meta
    assert registry["test_tool"][1] is test_tool


def test_tool_decorator_registers_aliases():
    meta = ToolMeta(
        name="primary",
        description="Primary tool",
        parameters={"type": "object", "properties": {}},
        category="test",
        display_name="Primary",
        aliases=("alias_a", "alias_b"),
    )

    @tool(meta)
    async def primary(arguments: dict) -> str:
        return "ok"

    registry = get_all_registered_tools()
    assert "primary" in registry
    assert "alias_a" in registry
    assert "alias_b" in registry
    # All point to same meta and handler
    assert registry["alias_a"][0] is meta
    assert registry["alias_b"][1] is primary


def test_clear_registry_empties_all():
    @tool(ToolMeta(
        name="temp", description="x", parameters={}, category="test", display_name="Temp",
    ))
    async def temp(args: dict) -> str:
        return ""

    assert len(get_all_registered_tools()) > 0
    clear_registry()
    assert len(get_all_registered_tools()) == 0


def test_tool_meta_frozen():
    import pytest

    meta = ToolMeta(
        name="frozen_test", description="x", parameters={}, category="test", display_name="X",
    )
    with pytest.raises(AttributeError):
        meta.name = "changed"  # type: ignore[misc]


def test_tool_meta_defaults():
    meta = ToolMeta(
        name="defaults", description="x", parameters={}, category="test", display_name="X",
    )
    assert meta.icon == "\U0001f527"
    assert meta.is_default is True
    assert meta.read_only is False
    assert meta.parallel_safe is False
    assert meta.governance == ""
    assert meta.pack == ""
    assert meta.aliases == ()
    assert meta.adapter == "request"
    assert meta.config == {}
    assert meta.config_schema == {}
