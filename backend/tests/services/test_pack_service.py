"""Tests for pack_service — catalog, agent packs, capability summary."""

import json
import uuid
from types import SimpleNamespace

from app.services.agent_tools import CORE_TOOL_NAMES
from app.services.pack_service import (
    KERNEL_TOOLS,
    _resolve_session_conversation_id,
    _summarize_chat_messages,
    collect_skill_declared_packs,
    get_pack_catalog,
)
from app.skills.types import ParsedSkill, SkillMetadata
from app.tools.packs import iter_tool_packs


def test_pack_catalog_returns_all_packs():
    catalog = get_pack_catalog()
    assert len(catalog) >= 4
    names = {p["name"] for p in catalog}
    assert "web_pack" in names
    assert "feishu_pack" in names
    assert "plaza_pack" in names
    assert "mcp_admin_pack" in names
    assert "email_pack" in names
    assert "deep_research_pack" in names
    assert "finance_pack" in names
    assert "office_pack" in names
    assert "document_pack" not in names
    assert "image_pack" not in names


def test_pack_catalog_has_required_fields():
    catalog = get_pack_catalog()
    for pack in catalog:
        assert "name" in pack
        assert "summary" in pack
        assert "source" in pack
        assert "tools" in pack
        assert "capabilities" in pack
        assert isinstance(pack["tools"], list)
        assert isinstance(pack["capabilities"], list)


def test_manifest_backed_packs_are_catalog_only():
    catalog = get_pack_catalog()
    finance = next(p for p in catalog if p["name"] == "finance_pack")

    assert finance["source"] == "manifest"
    assert finance["runtime_source_of_truth"] == "tool_decorator"
    assert "finance_compile_research_packet" in finance["tools"]
    assert "skills/secondary-equity-deep-dive" in finance["skills"]
    assert finance["credential_requirements"]
    assert finance["requires_channel"] is None


def test_pack_catalog_feishu_has_channel_dependency():
    catalog = get_pack_catalog()
    feishu = next(p for p in catalog if p["name"] == "feishu_pack")
    assert feishu["source"] == "channel"
    assert feishu["requires_channel"] == "feishu"
    assert len(feishu["capabilities"]) > 0


def test_pack_catalog_feishu_matches_current_tool_surface():
    catalog = get_pack_catalog()
    feishu = next(p for p in catalog if p["name"] == "feishu_pack")

    assert "feishu_base_app_create" in feishu["tools"]
    assert "feishu_base_record_delete" in feishu["tools"]
    assert "feishu_doc_delete" in feishu["tools"]
    assert "feishu_approval_create" in feishu["tools"]
    assert "feishu_approval_query" in feishu["tools"]
    assert "feishu_approval_get" in feishu["tools"]


def test_pack_catalog_system_pack_no_channel_dependency():
    catalog = get_pack_catalog()
    web = next(p for p in catalog if p["name"] == "web_pack")
    assert web["source"] == "system"
    assert web["requires_channel"] is None


def test_iter_tool_packs_hides_mcp_admin_pack_from_generic_queries():
    packs = iter_tool_packs()
    names = {pack.name for pack in packs}

    assert "web_pack" in names
    assert "mcp_admin_pack" not in names


def test_iter_tool_packs_returns_mcp_admin_pack_for_explicit_admin_queries():
    packs = iter_tool_packs("mcp")
    names = {pack.name for pack in packs}

    assert "mcp_admin_pack" in names


def test_plaza_pack_only_contains_real_shared_feed_tools():
    catalog = get_pack_catalog()
    plaza = next(p for p in catalog if p["name"] == "plaza_pack")

    assert plaza["source"] == "system"
    assert plaza["tools"] == [
        "plaza_get_new_posts",
        "plaza_create_post",
        "plaza_add_comment",
    ]
    assert "manage_tasks" not in plaza["tools"]
    assert "plaza_list_posts" not in plaza["tools"]
    assert "plaza_get_comments" not in plaza["tools"]
    assert "共享广场" in plaza["summary"]
    assert "协作" in plaza["activation_mode"]


def test_kernel_tools_are_strings():
    assert all(isinstance(t, str) for t in KERNEL_TOOLS)
    assert "read_file" in KERNEL_TOOLS
    assert "write_file" in KERNEL_TOOLS
    assert "load_skill" in KERNEL_TOOLS
    assert "tool_search" in KERNEL_TOOLS


def test_kernel_tools_match_runtime_core_tools():
    assert set(KERNEL_TOOLS) == set(CORE_TOOL_NAMES)
    assert "list_files" in KERNEL_TOOLS  # list_files is a core read-only tool
    assert "send_web_message" not in KERNEL_TOOLS


def test_resolve_session_conversation_id_always_uses_session_uuid():
    session_id = uuid.uuid4()
    session = SimpleNamespace(id=session_id, external_conv_id="feishu_p2p_ou_xxx")

    assert _resolve_session_conversation_id(session) == str(session_id)


def test_summarize_chat_messages_extracts_runtime_events_and_tool_usage():
    messages = [
        SimpleNamespace(
            role="system",
            content=json.dumps({
                "event_type": "pack_activation",
                "packs": [{"name": "web_pack"}],
                "message": "Activated web pack",
            }),
        ),
        SimpleNamespace(
            role="tool_call",
            content=json.dumps({
                "name": "read_file",
                "args": {"path": "skills/web-research/SKILL.md"},
                "status": "done",
                "result": "ok",
            }),
        ),
        SimpleNamespace(
            role="system",
            content=json.dumps({
                "event_type": "permission",
                "tool_name": "send_feishu_message",
                "status": "approval_required",
                "capability": "channel.feishu.message",
                "message": "This action requires approval.",
            }),
        ),
        SimpleNamespace(
            role="system",
            content=json.dumps({
                "event_type": "session_compact",
                "summary": "Older context compacted.",
            }),
        ),
    ]

    summary = _summarize_chat_messages(messages)

    assert summary == {
        "activated_packs": ["web_pack"],
        "used_tools": ["read_file"],
        "blocked_capabilities": [{
            "tool": "send_feishu_message",
            "status": "approval_required",
            "capability": "channel.feishu.message",
        }],
        "compaction_count": 1,
        "permission_event_count": 1,
        "team_memory_hit_count": 0,
        "last_compaction": {
            "summary": "Older context compacted.",
            "original_message_count": None,
            "kept_message_count": None,
            "continuity_sections_injected": [],
            "created_at": None,
        },
        "last_team_memory_hit": None,
        "last_tool_budget_event": None,
        "last_retry_reason": None,
    }


def test_collect_skill_declared_packs_merges_explicit_and_inferred_packs():
    skills = [
        ParsedSkill(
            metadata=SkillMetadata(
                name="Feishu Assistant",
                description="",
                declared_tools=("send_feishu_message",),
                declared_packs=("feishu_pack",),
            ),
            body="# Feishu Assistant",
            file_path=SimpleNamespace(),
            relative_path="skills/feishu/SKILL.md",
        ),
        ParsedSkill(
            metadata=SkillMetadata(
                name="Web Research",
                description="",
                declared_tools=("web_search", "firecrawl_fetch"),
                declared_packs=(),
            ),
            body="# Web Research",
            file_path=SimpleNamespace(),
            relative_path="skills/web/SKILL.md",
        ),
    ]

    declared = collect_skill_declared_packs(skills)

    assert declared == [
        {
            "name": "feishu_pack",
            "skills": ["Feishu Assistant"],
            "tools": ["send_feishu_message"],
        },
        {
            "name": "web_pack",
            "skills": ["Web Research"],
            "tools": ["firecrawl_fetch", "web_search"],
        },
    ]
