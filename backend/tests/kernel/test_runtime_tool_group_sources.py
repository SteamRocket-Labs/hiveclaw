"""Tool-frame notices use current schemas and exact code-owned metadata."""

from uuid import uuid4

from app.runtime.session import SessionContext
from app.runtime.context import ensure_runtime_assembly_state
from app.runtime.prompt_builder import build_dynamic_prompt_suffix
from app.services.governance_capability_taxonomy import capability_descriptor_for_name


def test_known_groups_are_constants_but_custom_summaries_keep_their_unknown_origin():
    descriptor = capability_descriptor_for_name("feishu_pack")
    group = {"name": descriptor.name, "summary": descriptor.notes, "tools": list(descriptor.tools)}
    for changed in (False, True):
        displayed = {**group, "summary": group["summary"] + (" PRIVATE_CUSTOM_METADATA" if changed else "")}
        sections = []
        rendered = build_dynamic_prompt_suffix(active_tool_groups=[displayed], context_section_ledger=sections)
        selected = next(row for row in sections if row["name"] == "active_tool_groups")
        assert selected["platform_constant"] is (not changed)
        assert ("PRIVATE_CUSTOM_METADATA" in rendered) is changed


def test_tool_refresh_cannot_advertise_revoked_cached_names_or_fabricate_their_source():
    from app.kernel.engine import _build_runtime_attachment_sections

    agent = uuid4()
    session = SessionContext(discovered_tools=["feishu_doc_read", "revoked_private_tool"])
    sections = _build_runtime_attachment_sections(agent, session,
        tools_for_llm=[{"type": "function", "function": {"name": "feishu_doc_read"}}])
    refresh = next(section for section in sections if section.startswith("## Runtime Tool Refresh"))
    assert "feishu_doc_read" in refresh and "revoked_private_tool" not in refresh
    state = ensure_runtime_assembly_state(session)
    assert state.context_sources_for(agent_id=agent, content=refresh) == ()
    assert state.context_unbound_for(agent_id=agent, content=refresh) == ()
    unverified = next(section for section in _build_runtime_attachment_sections(agent, session)
        if section.startswith("## Runtime Tool Refresh"))
    assert state.context_unbound_for(agent_id=agent, content=unverified) == ("context_source_inventory_missing",)
