"""Tests for pack_service — catalog, agent packs, capability summary."""

import json
import uuid
from types import SimpleNamespace

import pytest

from app.services.agent_tools import CORE_TOOL_NAMES
from app.services.pack_service import (
    KERNEL_TOOLS,
    get_capability_summary,
    _resolve_session_conversation_id,
    _summarize_chat_messages,
    collect_skill_declared_packs,
    get_pack_catalog,
)
from app.skills.types import ParsedSkill, SkillMetadata
from app.tools.runtime_tool_groups import iter_runtime_tool_groups


def test_pack_catalog_returns_all_packs():
    catalog = get_pack_catalog()
    assert len(catalog) >= 4
    names = {p["name"] for p in catalog}
    assert "web_pack" in names
    assert "feishu_pack" in names
    assert "plaza_pack" in names
    assert "mcp_admin_pack" in names
    assert "email_pack" in names
    assert "office_pack" in names
    removed_pack = "_".join(("deep", "research", "pack"))
    assert removed_pack not in names
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
    assert "feishu_approval_definition" in feishu["tools"]
    assert "feishu_approval_query" in feishu["tools"]
    assert "feishu_approval_get" in feishu["tools"]


def test_pack_catalog_system_pack_no_channel_dependency():
    catalog = get_pack_catalog()
    web = next(p for p in catalog if p["name"] == "web_pack")
    assert web["source"] == "system"
    assert web["requires_channel"] is None


def test_iter_runtime_tool_groups_hides_mcp_admin_pack_from_generic_queries():
    packs = iter_runtime_tool_groups()
    names = {pack.name for pack in packs}

    assert "web_pack" in names
    assert "mcp_admin_pack" not in names


def test_iter_runtime_tool_groups_returns_mcp_admin_pack_for_explicit_admin_queries():
    packs = iter_runtime_tool_groups("mcp")
    names = {pack.name for pack in packs}

    assert "mcp_admin_pack" in names


def test_iter_runtime_tool_groups_does_not_return_core_office_runtime_tools():
    exact_tool_aliases = {pack.name for pack in iter_runtime_tool_groups("office_document_create")}
    spaced_pack_aliases = {pack.name for pack in iter_runtime_tool_groups("office document")}

    assert "office_pack" not in exact_tool_aliases
    assert "office_pack" not in spaced_pack_aliases


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
    assert "Shared plaza feed" in plaza["summary"]
    assert "collaboration feed" in plaza["activation_mode"]


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
            content=json.dumps(
                {
                    "event_type": "pack_activation",
                    "packs": [{"name": "web_pack"}],
                    "message": "Activated web pack",
                }
            ),
        ),
        SimpleNamespace(
            role="tool_call",
            content=json.dumps(
                {
                    "name": "read_file",
                    "args": {"path": "skills/web-research/SKILL.md"},
                    "status": "done",
                    "result": "ok",
                }
            ),
        ),
        SimpleNamespace(
            role="system",
            content=json.dumps(
                {
                    "event_type": "permission",
                    "tool_name": "send_feishu_message",
                    "status": "approval_required",
                    "capability": "channel.feishu.message",
                    "message": "This action requires approval.",
                }
            ),
        ),
        SimpleNamespace(
            role="system",
            content=json.dumps(
                {
                    "event_type": "session_compact",
                    "summary": "Older context compacted.",
                }
            ),
        ),
    ]

    summary = _summarize_chat_messages(messages)

    assert summary == {
        "activated_tool_groups": ["web_pack"],
        "used_tools": ["read_file"],
        "blocked_capabilities": [
            {
                "tool": "send_feishu_message",
                "status": "approval_required",
                "capability": "channel.feishu.message",
            }
        ],
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
    assert "activated_packs" not in summary


class _ScalarResult:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, results):
        self._results = list(results)

    async def execute(self, _stmt):
        if not self._results:
            raise AssertionError("Unexpected execute() call")
        return self._results.pop(0)


@pytest.mark.asyncio
async def test_capability_summary_contract_omits_pack_fields_when_agent_missing():
    summary = await get_capability_summary(_FakeDB([_ScalarResult(None)]), uuid.uuid4())

    assert "available_packs" not in summary
    assert "channel_backed_packs" not in summary
    assert "skill_declared_packs" not in summary
    assert summary["capability_policies"] == []


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
            "tools": ["firecrawl_fetch"],
        },
    ]


def test_collect_skill_declared_packs_uses_metadata_pack_without_tool_inference_noise():
    skills = [
        ParsedSkill(
            metadata=SkillMetadata(
                name="Office Productivity",
                description="",
                declared_tools=("web_search", "firecrawl_fetch", "read_file"),
                declared_packs=(),
                pack="office_pack",
            ),
            body="# Office Productivity",
            file_path=SimpleNamespace(),
            relative_path="skills/office-productivity/SKILL.md",
        )
    ]

    declared = collect_skill_declared_packs(skills)

    assert declared == [
        {
            "name": "office_pack",
            "skills": ["Office Productivity"],
            "tools": ["firecrawl_fetch", "read_file", "web_search"],
        },
        {
            "name": "web_pack",
            "skills": ["Office Productivity"],
            "tools": ["firecrawl_fetch"],
        },
    ]


def test_office_pack_is_manifest_only_and_does_not_own_core_runtime_tools():
    from app.services.pack_service import get_pack_catalog
    from app.tools.runtime_tool_groups import runtime_tool_group_for_name

    runtime_pack = runtime_tool_group_for_name("office_pack")
    catalog_pack = next(pack for pack in get_pack_catalog() if pack["name"] == "office_pack")

    assert runtime_pack is None
    assert catalog_pack["source"] == "manifest"
    assert catalog_pack["owns"] == []
    assert set(catalog_pack["requires_core"]) >= {
        "read_document",
        "office_document_create",
        "office_document_view",
        "office_document_query",
        "office_document_apply",
        "office_document_validate",
        "office_document_dump",
    }
