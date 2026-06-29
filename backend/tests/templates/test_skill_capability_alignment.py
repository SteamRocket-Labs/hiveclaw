"""Capability alignment + best-practice tests for all SKILL.md templates (PR-31).

These tests guard the Group B skill rewrites against two classes of regression:

1. **Capability drift**: a skill body references tool names that don't exist
   on the agent's actual tool surface. The body lists tools the LLM thinks
   it can call — if those names don't match reality, the agent wastes
   rounds calling non-existent tools or hallucinating parameters.

2. **Best-practice drift**: a skill body loses the XML structure, anti-
   patterns section, or Input→Output examples that the Group B upgrade
   introduced. Without these, skill quality degrades silently over time.

Test sources:
- `app/templates/skills/*/SKILL.md`        — user skills (is_default toggle)
- `app/templates/system_skills/*/SKILL.md` — system skills (always-installed)
- `agent_template/skills/*/SKILL.md`       — default agent template skills
- `hr_agent_template/skills/*/SKILL.md`    — HR agent template skills

For each skill file we verify:
- Frontmatter is parseable
- Tool names referenced in the body either (a) appear in frontmatter
  `tools:`, (b) are CORE_TOOL_NAMES (always available), or (c) are on
  the explicit cross-skill whitelist.
- If the skill has been upgraded to Group B best-practice, body contains
  the required XML sub-blocks and a non-trivial anti_patterns section.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services.agent_tools import CORE_TOOL_NAMES
from app.skills.parser import SkillParser

_BACKEND_ROOT = Path(__file__).resolve().parents[2]

# Tools the LLM may legitimately reference even when they aren't in the
# current skill's frontmatter:
# - They are in CORE_TOOL_NAMES (always available)
# - They are in this explicit cross-skill allowlist (documented cross-pack
#   references, such as a research skill pointing at a messaging tool).
_CROSS_SKILL_ALLOWLIST: set[str] = {
    # Legitimate cross-skill references that appear in multiple SKILL.md
    # bodies for workflow-pairing documentation purposes.
    "send_feishu_message",  # referenced by integration playbooks
    "feishu_user_search",  # referenced by feishu-integration + others
    "read_document",  # structured reader, referenced by office/document skills
    "send_email",  # email-guide workflow reference
    "read_emails",  # email-guide workflow reference
    "reply_email",  # email-guide workflow reference
    "plaza_get_new_posts",  # referenced in anti-patterns
    "plaza_create_post",  # referenced in anti-patterns
    "plaza_add_comment",  # referenced in anti-patterns
    "feishu_sheet_info",  # office-productivity cross-references
    "feishu_sheet_read",  # office-productivity cross-references
    "update_trigger",  # integration examples may reference trigger maintenance
    "cancel_trigger",  # same
    "preview_agent_blueprint",  # hr skill tool
    "create_digital_employee",  # hr skill tool
    "discover_resources",  # mcp skill tool
    "search_clawhub",  # hr skill tool
    "import_mcp_server",  # mcp skill tool
    "list_mcp_resources",  # mcp skill tool
    "read_mcp_resource",  # mcp skill tool
    "feishu_doc_create",  # cross-reference to Feishu Integration
    "feishu_doc_append",  # cross-reference to Feishu Integration
}


# Any bare identifier in backticks (`name_here`) inside a skill body is
# treated as a potential tool reference if it looks like snake_case.
_BACKTICK_IDENT_RE = re.compile(r"`([a-z][a-z0-9_]*[a-z0-9])`")

# Also match tool-call syntax like `name_here(...)` since some prompts
# write calls directly.
_TOOL_CALL_RE = re.compile(r"`([a-z][a-z0-9_]*[a-z0-9])\(")


# Identifiers that look snake_case but are NOT tools — parameter names,
# field types, enum values, well-known non-tool vocabulary. Exclude these
# from the "is it a declared tool?" check.
_NON_TOOL_IDENTIFIERS: set[str] = {
    # Frontmatter keys
    "name",
    "description",
    "tools",
    "packs",
    "license",
    "metadata",
    "version",
    "category",
    "is_system",
    "compatibility",
    # Common parameter names used in doc tables
    "tool_name",
    "msg_type",
    "approval_code",
    "user_id",
    "open_id",
    "email",
    "member_names",
    "member_open_ids",
    "document_token",
    "spreadsheet_token",
    "spreadsheet_url",
    "sheet_id",
    "range",
    "value_render_option",
    "base_token",
    "table_id",
    "view_id",
    "offset",
    "limit",
    "field_name",
    "type",
    "property",
    "record_id",
    "fields",
    "field_id",
    "file_path",
    "summary",
    "description",
    "assignee_open_id",
    "due",
    "tasklist_id",
    "idempotency_key",
    "task_id",
    "content",
    "action",
    "permission",
    "title",
    "folder_token",
    "max_chars",
    "node_token",
    "recursive",
    "instance_id",
    "status",
    "form",
    "start_time",
    "end_time",
    "timezone",
    "location",
    "attendee_names",
    "attendee_emails",
    "attendee_open_ids",
    "user_open_id",
    "user_email",
    "max_results",
    "event_id",
    "agent_name",
    "message",
    "at",
    "reason",
    "trace_id",
    "parent_session_id",
    "time_zone",
    "page_all",
    "page_limit",
    "complete",
    "due_start",
    "due_end",
    "created_at",
    "query",
    "members",
    "max_select",
    "max_tokens",
    "temperature",
    "member_name",
    "app_id",
    "app_secret",
    # Enum-ish / values
    "running",
    "completed",
    "failed",
    "once",
    "add",
    "remove",
    "list",
    "view",
    "edit",
    "full_access",
    "consult",
    "notify",
    "cron",
    "approved",
    "rejected",
    "pending",
    # File names / paths / common dir names
    "relationships",
    "focus",
    "soul",
    "heartbeat",
    "dream",
    "workspace",
    "memory",
    "skills",
    "evolution",
    "logs",
    "tests",
    # MCP vocabulary
    "mcp_server",
    "prompt_name",
    "rpc",
    "json",
    "yaml",
    # Generic tokens
    "true",
    "false",
    "none",
    "null",
    "input",
    "output",
    # Trigger config names (not tools)
    "from_agent_id",
    "from_user_identity",
    "on_message",
    "reply_to_current_sender",
    "interval_min",
    "fire_on",
    "blocked_pattern",
    "trigger_class",
    "scheduled_job",
    "event_wait",
    "system_maintenance",
    # Parameter/field names surfaced in tool-reference tables
    "max_tool_rounds",
    "message_id",
    "post_id",
    "task_id",
    "spreadsheet_url",
    "search",
    "attachments",
    "to",
    "subject",
    "body",
    "cc",
    "sheet_size",
    "tz",
    "expr",
    "minutes",
    # MCP import config keys
    "mcp_url",
    "server_id",
    "api_key",
    "smithery_api_key",
    # Blueprint schema keys (HR CREATE_EMPLOYEE skill)
    "role_description",
    "primary_users",
    "core_outputs",
    "focus_content",
    "welcome_message",
    "heartbeat_topics",
    "permission_scope",
    "skill_names",
    "clawhub_slugs",
    "external_skill_urls",
    "triggers",
    "personality",
    "boundaries",
    "first_tasks",
    # Body section tokens that might match regex
    "role",
    "when_to_use",
    "do_not_use_when",
    "tool_reference",
    "workflows",
    "examples",
    "anti_patterns",
    "success_criteria",
    "output_format",
    "hard_rules",
    "pipeline_context",
    "output_contract",
}


def _discover_skill_files() -> list[Path]:
    """Find every SKILL.md under template and agent-template skill roots."""
    paths: list[Path] = []
    paths.extend(sorted((_BACKEND_ROOT / "app" / "templates" / "skills").glob("*/SKILL.md")))
    paths.extend(sorted((_BACKEND_ROOT / "app" / "templates" / "system_skills").glob("*/SKILL.md")))
    for template_root_name in ("agent_template", "hr_agent_template"):
        skills_dir = _BACKEND_ROOT / template_root_name / "skills"
        if skills_dir.is_dir():
            paths.extend(sorted(skills_dir.glob("*/SKILL.md")))
    return paths


_SKILL_FILES = _discover_skill_files()

_MANAGED_CREDENTIAL_BOUNDARY_SKILLS: dict[str, tuple[str, ...]] = {
    "app/templates/system_skills/dingtalk-integration/SKILL.md": (
        "do not inspect environment variables",
        "run_command",
        "configuration gap",
    ),
    "app/templates/system_skills/email-guide/SKILL.md": (
        "do not inspect environment variables",
        "run_command",
        "configuration gap",
    ),
    "app/templates/system_skills/feishu-integration/SKILL.md": (
        "do not inspect environment variables",
        "run_command",
        "channel config",
    ),
    "app/templates/system_skills/web-research/SKILL.md": (
        "do not inspect environment variables",
        "run_command",
        "configuration gap",
    ),
}


def _extract_candidate_tool_references(body: str) -> set[str]:
    """Pull every backtick-wrapped snake_case identifier out of a skill body."""
    candidates: set[str] = set()
    for match in _BACKTICK_IDENT_RE.finditer(body):
        candidates.add(match.group(1))
    for match in _TOOL_CALL_RE.finditer(body):
        candidates.add(match.group(1))
    return {name for name in candidates if name not in _NON_TOOL_IDENTIFIERS}


def _is_likely_tool_name(name: str) -> bool:
    """Heuristic: snake_case with at least one underscore, not a pure noun."""
    if "_" not in name:
        return False
    if name in _NON_TOOL_IDENTIFIERS:
        return False
    return True


@pytest.fixture(scope="module")
def parser() -> SkillParser:
    return SkillParser()


class TestSkillDiscovery:
    def test_found_skill_files(self) -> None:
        # Retired default guides and single-purpose Office copies should not be counted.
        assert len(_SKILL_FILES) >= 8, f"expected at least 8 skill files, found {len(_SKILL_FILES)}"

    def test_every_file_is_parseable(self, parser: SkillParser) -> None:
        for path in _SKILL_FILES:
            rel = str(path.relative_to(_BACKEND_ROOT))
            parsed = parser.parse_file(path, relative_path=rel)
            assert parsed.metadata.name, f"{rel} has no parseable name"
            assert parsed.body, f"{rel} has no body after frontmatter"

    def test_skill_templates_do_not_instruct_managed_channel_env_credentials(self) -> None:
        from app.services.managed_capability_guard import detect_managed_credential_guidance

        offenders: list[str] = []
        for path in _SKILL_FILES:
            rel = str(path.relative_to(_BACKEND_ROOT))
            content = path.read_text(encoding="utf-8")
            if detect_managed_credential_guidance(content):
                offenders.append(rel)

        assert not offenders, (
            "Skill templates must use platform channel configuration and dedicated tools, "
            f"not managed credential env setup/probing: {offenders}"
        )

    def test_managed_capability_skills_state_credential_boundary(self) -> None:
        for rel, required_phrases in _MANAGED_CREDENTIAL_BOUNDARY_SKILLS.items():
            content = (_BACKEND_ROOT / rel).read_text(encoding="utf-8").lower()
            missing = [phrase for phrase in required_phrases if phrase not in content]
            assert not missing, (
                f"{rel}: managed capability skills must state the platform credential boundary. "
                f"Missing phrases: {missing}"
            )

    def test_skill_installation_guides_do_not_instruct_blocked_shell_network_paths(self, parser: SkillParser) -> None:
        marketplace = _BACKEND_ROOT / "app" / "templates" / "skills" / "skill-marketplace" / "SKILL.md"

        marketplace_content = marketplace.read_text(encoding="utf-8")
        parsed = parser.parse_file(
            marketplace,
            relative_path="app/templates/skills/skill-marketplace/SKILL.md",
        )

        assert "import subprocess" not in marketplace_content
        assert "subprocess.run" not in marketplace_content

        assert "execute_code` with `curl" not in marketplace_content
        assert 'curl -s "https://api.github.com/repos/OWNER/REPO"' not in marketplace_content
        assert "jq '{stars:" not in marketplace_content
        assert {"web_search", "web_fetch", "firecrawl_fetch", "execute_code"} <= set(parsed.metadata.declared_tools)


@pytest.mark.parametrize("skill_path", _SKILL_FILES, ids=lambda p: str(p.relative_to(_BACKEND_ROOT)))
class TestCapabilityAlignment:
    """Every tool name referenced in a body must be accounted for."""

    def test_declared_tools_are_mentioned_in_body(self, parser: SkillParser, skill_path: Path) -> None:
        rel = str(skill_path.relative_to(_BACKEND_ROOT))
        parsed = parser.parse_file(skill_path, relative_path=rel)
        declared = set(parsed.metadata.declared_tools)
        if not declared:
            pytest.skip(f"{rel} has no declared tools (likely MCP-dynamic or pure guide)")
        body = parsed.body
        unmentioned = [t for t in declared if t not in body]
        assert not unmentioned, (
            f"{rel}: declared tools not referenced anywhere in body: {unmentioned}. "
            "Either reference them or remove from frontmatter."
        )

    def test_body_tool_references_are_declared_or_allowed(self, parser: SkillParser, skill_path: Path) -> None:
        rel = str(skill_path.relative_to(_BACKEND_ROOT))
        parsed = parser.parse_file(skill_path, relative_path=rel)
        declared = set(parsed.metadata.declared_tools)
        body = parsed.body
        candidates = _extract_candidate_tool_references(body)
        suspicious = [
            name
            for name in sorted(candidates)
            if _is_likely_tool_name(name)
            and name not in declared
            and name not in CORE_TOOL_NAMES
            and name not in _CROSS_SKILL_ALLOWLIST
        ]
        # Allow one explicit escape hatch: prefix-based dynamic tools. Those are
        # marked by appearing with a trailing "*" elsewhere in the body.
        dynamic_prefixes = {m.group(1) for m in re.finditer(r"`([a-z][a-z0-9_]*)_\*`", body)}
        suspicious = [
            name for name in suspicious if not any(name.startswith(prefix + "_") for prefix in dynamic_prefixes)
        ]
        assert not suspicious, (
            f"{rel}: body references tools that are not in frontmatter tools:, "
            f"CORE_TOOL_NAMES, or the cross-skill allowlist: {suspicious}. "
            "Either declare them in the frontmatter tools: list, add to "
            "_CROSS_SKILL_ALLOWLIST with a justification, or rename."
        )


@pytest.mark.parametrize("skill_path", _SKILL_FILES, ids=lambda p: str(p.relative_to(_BACKEND_ROOT)))
class TestBestPracticeStructure:
    """After Group B rewrites, every skill body must satisfy best-practice
    minima: role, when_to_use, anti_patterns, at least one Input→Output
    example."""

    _REQUIRED_XML_TAGS = (
        "<role>",
        "</role>",
        "<when_to_use>",
        "</when_to_use>",
        "<anti_patterns>",
        "</anti_patterns>",
    )

    def test_required_xml_tags_present(self, parser: SkillParser, skill_path: Path) -> None:
        rel = str(skill_path.relative_to(_BACKEND_ROOT))
        parsed = parser.parse_file(skill_path, relative_path=rel)
        body = parsed.body
        missing = [tag for tag in self._REQUIRED_XML_TAGS if tag not in body]
        assert not missing, (
            f"{rel}: missing required XML tags {missing}. "
            "Group B upgrade requires <role>, <when_to_use>, <anti_patterns>."
        )

    def test_anti_patterns_section_non_trivial(self, parser: SkillParser, skill_path: Path) -> None:
        rel = str(skill_path.relative_to(_BACKEND_ROOT))
        parsed = parser.parse_file(skill_path, relative_path=rel)
        body = parsed.body
        start = body.find("<anti_patterns>")
        end = body.find("</anti_patterns>")
        assert start >= 0 and end > start, f"{rel}: <anti_patterns> block malformed"
        section = body[start:end]
        # Count distinct bullets (❌ markers OR leading dashes with meaningful content).
        bullet_count = section.count("❌") + section.count("\n- ")
        assert bullet_count >= 3, (
            f"{rel}: anti_patterns has only {bullet_count} bullets; need at least 3 concrete failure modes."
        )

    def test_has_input_output_example_or_tool_reference_table(self, parser: SkillParser, skill_path: Path) -> None:
        rel = str(skill_path.relative_to(_BACKEND_ROOT))
        parsed = parser.parse_file(skill_path, relative_path=rel)
        body = parsed.body
        has_examples_block = "<examples>" in body and "</examples>" in body
        has_tool_table = "| Tool" in body or "| I need to" in body
        has_workflow = "<workflow" in body or "## Workflow" in body or "### " in body
        assert has_examples_block or (has_tool_table and has_workflow), (
            f"{rel}: must have either <examples> block OR a tool-reference "
            "table + at least one workflow section (LLM needs concrete shape)."
        )


class TestCoreToolNamesInvariant:
    """CORE_TOOL_NAMES are always available — the test framework depends on
    this set being stable. If this list changes, the cross-skill allowlist
    may need review."""

    def test_core_tool_names_contains_expected_baseline(self) -> None:
        baseline = {
            "read_file",
            "write_file",
            "list_files",
            "search_memory",
            "load_memory",
            "save_memory",
            "get_current_time",
            "load_skill",
            "tool_search",
            "save_skill",
            "send_message_to_agent",
            "delegate_to_agent",
            "check_async_task",
            "cancel_async_task",
            "set_trigger",
            "list_triggers",
            "send_channel_file",
        }
        missing = baseline - CORE_TOOL_NAMES
        assert not missing, (
            f"CORE_TOOL_NAMES no longer includes these baseline tools: {missing}. "
            "The skill-cross-reference test relies on them being always-on."
        )
