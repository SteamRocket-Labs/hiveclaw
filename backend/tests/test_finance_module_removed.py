from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


FINANCE_SOURCE_PATHS = (
    REPO_ROOT / "backend" / "app" / "finance_analysis",
    REPO_ROOT / "backend" / "app" / "finance_data",
    REPO_ROOT / "backend" / "app" / "tools" / "handlers" / "finance.py",
    REPO_ROOT / "backend" / "app" / "templates" / "system_skills" / "finance-research",
    REPO_ROOT / "backend" / "packs" / "finance_pack",
    REPO_ROOT / "packs" / "finance_pack",
)


def test_finance_source_trees_are_removed() -> None:
    existing = [path.relative_to(REPO_ROOT).as_posix() for path in FINANCE_SOURCE_PATHS if path.exists()]

    assert existing == []


def test_finance_tools_and_pack_are_not_registered() -> None:
    from app.services.capability_gate import CAPABILITY_MAP
    from app.services.pack_service import get_pack_catalog
    from app.tools.collector import HANDLER_MODULES, collect_tools
    from app.tools.runtime_tool_groups import RUNTIME_TOOL_GROUPS

    collected = collect_tools()
    registered_tool_names = {tool["function"]["name"] for tool in collected.openai_tools}
    static_pack_names = {pack.name for pack in RUNTIME_TOOL_GROUPS}
    catalog_names = {pack["name"] for pack in get_pack_catalog()}

    assert "app.tools.handlers.finance" not in HANDLER_MODULES
    assert not any(name.startswith("finance_") for name in registered_tool_names)
    assert "finance_pack" not in collected.pack_tool_groups
    assert "finance_pack" not in static_pack_names
    assert "finance_pack" not in catalog_names
    assert not any(name.startswith("finance_") for name in CAPABILITY_MAP)
