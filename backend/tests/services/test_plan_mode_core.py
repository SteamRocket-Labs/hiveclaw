"""Unit tests for the Plan Mode functional core (pure, no DB / no IO).

Covers the security-critical pure logic from the design doc:
  - §6.3 plan_json skeleton + schema validation
  - §7 state machine transition legality
  - §8.1 self-confirm prohibition
  - §8.2 version/hash binding on confirmation
  - sha256 canonical hashing (deterministic, key-order independent)
  - §6.2 markdown frontmatter mirroring DB fields
"""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest


def test_plan_mode_acceptance_reply_only_matches_bare_acceptance():
    from app.services.plan_mode_core import is_plan_mode_acceptance_reply

    assert is_plan_mode_acceptance_reply("进入计划模式")
    assert is_plan_mode_acceptance_reply("同意进入计划模式。")
    assert not is_plan_mode_acceptance_reply("先做计划：完整调研这个行业并出报告")
    assert not is_plan_mode_acceptance_reply("进入计划模式，帮我重新规划另一个任务")


def test_explicit_plan_mode_does_not_seed_retired_manage_tasks_tool():
    from app.services.plan_mode_core import classify_plan_mode_entry

    decision = classify_plan_mode_entry("进入计划模式，帮我规划这个调研", explicit=True)

    assert decision.mode == "explicit"
    assert decision.intent_type == "in_session_execution"
    assert decision.action_kind == "start_long_task"
    assert decision.tool_name == "continue_current_session"


def test_extract_plan_confirmation_request_matches_explicit_plan_id_and_latest():
    from app.services.plan_mode_core import extract_plan_confirmation_request

    plan_id = "a7cdfa75-cec5-4062-8bda-b18b2d2821a3"

    explicit = extract_plan_confirmation_request(f"确认 plan_id={plan_id}")
    assert explicit is not None
    assert explicit.plan_id == plan_id
    assert explicit.latest is False

    latest = extract_plan_confirmation_request("确认上一个计划")
    assert latest is not None
    assert latest.plan_id is None
    assert latest.latest is True

    assert extract_plan_confirmation_request("确认一下收件人是谁") is None


# ---------------------------------------------------------------------------
# Canonical hashing
# ---------------------------------------------------------------------------


def test_canonical_plan_json_is_key_order_independent():
    from app.services.plan_mode_core import canonical_plan_json

    a = {"title": "x", "steps": [{"order": 1, "description": "d"}], "schema": "hive_plan.v1"}
    b = {"schema": "hive_plan.v1", "steps": [{"description": "d", "order": 1}], "title": "x"}

    assert canonical_plan_json(a) == canonical_plan_json(b)


def test_compute_plan_hash_matches_sha256_of_canonical_json():
    from app.services.plan_mode_core import canonical_plan_json, compute_plan_hash

    plan = {"schema": "hive_plan.v1", "title": "x"}
    expected = "sha256:" + hashlib.sha256(canonical_plan_json(plan).encode("utf-8")).hexdigest()

    assert compute_plan_hash(plan) == expected


def test_compute_plan_hash_changes_when_content_changes():
    from app.services.plan_mode_core import compute_plan_hash

    base = {"schema": "hive_plan.v1", "title": "Daily brief", "objective": "a"}
    edited = {"schema": "hive_plan.v1", "title": "Daily brief", "objective": "b"}

    assert compute_plan_hash(base) != compute_plan_hash(edited)


# ---------------------------------------------------------------------------
# Skeleton generation (§6.3 / §10.2)
# ---------------------------------------------------------------------------


def test_build_plan_skeleton_includes_all_required_fields():
    from app.services.plan_mode_core import REQUIRED_PLAN_FIELDS, build_plan_skeleton

    skeleton = build_plan_skeleton(
        intent_type="autonomous_wake",
        title="Daily industry news brief",
        original_request="每天 9 点帮我整理新闻",
    )

    for field in REQUIRED_PLAN_FIELDS:
        assert field in skeleton, f"missing required field: {field}"

    assert skeleton["schema"] == "hive_plan.v1"
    assert skeleton["intent_type"] == "autonomous_wake"
    assert skeleton["title"] == "Daily industry news brief"
    # Steps/criteria are present as lists (possibly empty for the LLM to fill).
    assert isinstance(skeleton["steps"], list)
    assert isinstance(skeleton["success_criteria"], list)
    assert isinstance(skeleton["stop_conditions"], list)


def test_build_plan_skeleton_autonomous_wake_seeds_cron_wake_policy():
    from app.services.plan_mode_core import build_plan_skeleton

    skeleton = build_plan_skeleton(
        intent_type="autonomous_wake",
        title="t",
        original_request="r",
    )
    assert skeleton["wake_policy"]["type"] == "cron"
    assert skeleton["handoff"]["target"] == "scheduled_trigger"
    # Exec/automation CC-align: a recurring plan creates a trigger directly,
    # with no intermediate objective row.
    assert "create_objective" not in skeleton["handoff"]
    assert skeleton["handoff"]["create_trigger"] is True


def test_build_plan_skeleton_long_task_targets_continuation_handoff():
    from app.services.plan_mode_core import build_plan_skeleton

    skeleton = build_plan_skeleton(intent_type="in_session_execution", title="t", original_request="r")
    # G/H.2: a long_task intent seeds the live-session continuation target (not the
    # ambiguous legacy ``"long_task"`` target word). The legacy target stays
    # *registered* for already-persisted plans (see test_plan_mode_registry).
    assert skeleton["handoff"]["target"] == "continue_current_session"
    # A long task is not a recurring wake policy.
    assert skeleton["wake_policy"]["type"] == "none"


def test_build_plan_skeleton_rejects_unknown_intent_type():
    from app.services.plan_mode_core import build_plan_skeleton

    with pytest.raises(ValueError, match="intent_type"):
        build_plan_skeleton(intent_type="not_a_real_intent", title="t", original_request="r")


def test_delegate_tool_args_seed_delegation_handoff_payload():
    from app.services.plan_mode_core import tool_args_to_plan_fill

    fill = tool_args_to_plan_fill(
        tool_name="delegate_to_agent",
        action_kind="start_delegation",
        arguments={
            "agent_name": "投研助理",
            "message": "分析最近三天 AI Infra 融资动态。",
            "tool_profile": "research_readonly",
            "max_tool_rounds": 16,
        },
    )

    assert fill["title"] == "Delegate to 投研助理"
    assert fill["handoff"]["target"] == "delegation"
    assert fill["handoff"]["payload"]["agent_name"] == "投研助理"
    assert fill["handoff"]["payload"]["message"] == "分析最近三天 AI Infra 融资动态。"
    assert fill["handoff"]["payload"]["tool_profile"] == "research_readonly"
    assert fill["handoff"]["payload"]["max_tool_rounds"] == 16


# ---------------------------------------------------------------------------
# Schema validation (§10.2 step 3)
# ---------------------------------------------------------------------------


def test_validate_plan_json_accepts_well_formed_plan():
    from app.services.plan_mode_core import build_plan_skeleton, validate_plan_json

    plan = build_plan_skeleton(intent_type="autonomous_wake", title="t", original_request="r")
    plan["objective"] = "Produce a useful daily brief."
    plan["steps"] = [{"order": 1, "description": "Collect sources", "expected_output": "list"}]
    plan["success_criteria"] = ["Brief has 5-10 updates with links."]
    plan["stop_conditions"] = ["User cancels."]

    assert validate_plan_json(plan) == []


def test_validate_plan_json_rejects_wrong_schema_tag():
    from app.services.plan_mode_core import build_plan_skeleton, validate_plan_json

    plan = build_plan_skeleton(intent_type="in_session_execution", title="t", original_request="r")
    plan["schema"] = "something.else"

    errors = validate_plan_json(plan)
    assert any("schema" in e for e in errors)


def test_validate_plan_json_rejects_missing_required_fields():
    from app.services.plan_mode_core import validate_plan_json

    errors = validate_plan_json({"schema": "hive_plan.v1", "title": "t"})
    assert errors  # many missing fields
    assert any("objective" in e for e in errors)


def test_validate_plan_json_rejects_malformed_step():
    from app.services.plan_mode_core import build_plan_skeleton, validate_plan_json

    plan = build_plan_skeleton(intent_type="in_session_execution", title="t", original_request="r")
    plan["objective"] = "obj"
    plan["steps"] = [{"description": "missing order"}]  # no `order`
    plan["success_criteria"] = ["c"]
    plan["stop_conditions"] = ["s"]

    errors = validate_plan_json(plan)
    assert any("order" in e for e in errors)


def test_validate_plan_json_rejects_risk_level_out_of_enum():
    from app.services.plan_mode_core import build_plan_skeleton, validate_plan_json

    plan = build_plan_skeleton(intent_type="in_session_execution", title="t", original_request="r")
    plan["objective"] = "obj"
    plan["steps"] = [{"order": 1, "description": "d"}]
    plan["success_criteria"] = ["c"]
    plan["stop_conditions"] = ["s"]
    plan["risk_assessment"] = {"level": "apocalyptic", "reasons": []}

    errors = validate_plan_json(plan)
    assert any("risk" in e for e in errors)


# ── CC alignment §4.1: plan_markdown is the body + meta-steps rejected ──


def test_build_plan_skeleton_includes_plan_markdown_field():
    from app.services.plan_mode_core import build_plan_skeleton

    skeleton = build_plan_skeleton(intent_type="in_session_execution", title="t", original_request="r")
    assert "plan_markdown" in skeleton
    assert skeleton["plan_markdown"] == ""


def test_validate_plan_json_rejects_plan_mode_meta_steps():
    from app.services.plan_mode_core import build_plan_skeleton, validate_plan_json

    plan = build_plan_skeleton(intent_type="in_session_execution", title="t", original_request="r")
    plan["objective"] = "出 RWA 周报"
    plan["success_criteria"] = ["c"]
    plan["stop_conditions"] = ["s"]
    plan["steps"] = [
        {"order": 1, "description": "调用 exit_plan_mode 提交计划卡片"},
        {"order": 2, "description": "等待用户确认"},
    ]

    errors = validate_plan_json(plan)
    assert any("meta-step" in e for e in errors), errors


def test_validate_plan_json_accepts_real_execution_steps():
    # Guard against false positives: legitimate steps that merely contain "等待"
    # (but not "等待确认") or "submit" (but not "submit the plan") must pass.
    from app.services.plan_mode_core import build_plan_skeleton, validate_plan_json

    plan = build_plan_skeleton(intent_type="in_session_execution", title="t", original_request="r")
    plan["objective"] = "出 RWA 周报"
    plan["success_criteria"] = ["c"]
    plan["stop_conditions"] = ["s"]
    plan["steps"] = [
        {"order": 1, "description": "收集 RWA 赛道来源并核验，等待数据源返回后整理"},
        {"order": 2, "description": "撰写周报草稿并 submit the report to the user"},
    ]

    assert validate_plan_json(plan) == []


def test_render_plan_markdown_uses_agent_authored_body_when_present():
    from uuid import uuid4

    from app.services.plan_mode_core import build_plan_skeleton, render_plan_markdown

    plan = build_plan_skeleton(intent_type="in_session_execution", title="RWA 周报", original_request="r")
    plan["objective"] = "出 RWA 周报"
    plan["plan_markdown"] = "## 思路\n聚焦三条赛道，给出投资视角。\n\n## 执行\n1. 核验来源。"

    md = render_plan_markdown(
        plan_id=uuid4(),
        agent_id=uuid4(),
        tenant_id=None,
        status="awaiting_confirmation",
        plan_version=1,
        plan_hash="sha256:abc",
        intent_type="in_session_execution",
        created_at="2026-06-08T00:00:00+00:00",
        plan_json=plan,
    )
    # The agent's article is the body verbatim — not a re-stitch of structured fields.
    assert "聚焦三条赛道，给出投资视角。" in md
    assert "## 执行" in md
    # Frontmatter (canonical DB mirror) still leads the file.
    assert "plan_hash: sha256:abc" in md
    # Structured "## Wake policy" / "## Estimated cost" plumbing NOT stitched into
    # the body when the agent authored a plan.
    assert "## Wake policy" not in md


def test_render_plan_markdown_falls_back_to_structured_when_no_body():
    # Machine-intercepted plans have no authored article → keep the structured render.
    from uuid import uuid4

    from app.services.plan_mode_core import build_plan_skeleton, render_plan_markdown

    plan = build_plan_skeleton(intent_type="in_session_execution", title="t", original_request="r")
    plan["objective"] = "obj"

    md = render_plan_markdown(
        plan_id=uuid4(),
        agent_id=uuid4(),
        tenant_id=None,
        status="awaiting_confirmation",
        plan_version=1,
        plan_hash="sha256:abc",
        intent_type="in_session_execution",
        created_at="2026-06-08T00:00:00+00:00",
        plan_json=plan,
    )
    assert "## Objective" in md
    assert "## Steps" in md


def test_normalize_plan_json_for_validation_repairs_common_agent_planner_shape():
    from app.services.plan_mode_core import (
        build_plan_skeleton,
        normalize_plan_json_for_validation,
        validate_plan_json,
    )

    plan = build_plan_skeleton(intent_type="in_session_execution", title="Web3 full report", original_request="r")
    plan["objective"] = "Produce a structured Web3 panorama report."
    plan["steps"] = [
        {"description": "Frame the research scope."},
        {"title": "Map the main narratives."},
        "Synthesize findings into a final report.",
    ]
    plan["success_criteria"] = ["The user can review the plan before execution."]
    plan["stop_conditions"] = ["The user rejects the plan."]
    plan["risk_assessment"] = {"level": "moderate", "reasons": []}

    normalized = normalize_plan_json_for_validation(plan)

    assert [step["order"] for step in normalized["steps"]] == [1, 2, 3]
    assert normalized["steps"][1]["description"] == "Map the main narratives."
    assert normalized["risk_assessment"]["level"] == "medium"
    assert "non-standard risk level" in normalized["risk_assessment"]["reasons"][0]
    assert validate_plan_json(normalized) == []


# ---------------------------------------------------------------------------
# State machine (§7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "src,dst",
    [
        ("draft", "planning"),
        ("planning", "awaiting_confirmation"),
        ("planning", "planning_failed"),
        ("awaiting_confirmation", "confirmed"),
        ("awaiting_confirmation", "planning"),  # user requests revision
        ("awaiting_confirmation", "rejected"),
        ("awaiting_confirmation", "expired"),
        ("awaiting_confirmation", "superseded"),
        ("planning_failed", "planning"),  # retry
        ("planning_failed", "rejected"),  # user can dismiss a failed draft
    ],
)
def test_can_transition_allows_legal_edges(src, dst):
    from app.services.plan_mode_core import can_transition

    assert can_transition(src, dst) is True


@pytest.mark.parametrize(
    "src,dst",
    [
        ("planning", "confirmed"),  # agent self-confirm path forbidden
        ("draft", "confirmed"),  # skip planning + confirmation
        ("draft", "awaiting_confirmation"),  # skip planning
        ("confirmed", "awaiting_confirmation"),  # confirmed is immutable
        ("confirmed", "planning"),  # cannot re-plan a confirmed in place
        ("rejected", "confirmed"),  # terminal
        ("expired", "confirmed"),  # terminal
        ("superseded", "confirmed"),  # terminal
        ("confirmed", "rejected"),  # confirmed is terminal w.r.t. status
    ],
)
def test_can_transition_blocks_illegal_edges(src, dst):
    from app.services.plan_mode_core import can_transition

    assert can_transition(src, dst) is False


def test_terminal_statuses_have_no_outgoing_edges():
    from app.services.plan_mode_core import TERMINAL_STATUSES, can_transition

    for terminal in TERMINAL_STATUSES:
        for target in ("planning", "awaiting_confirmation", "confirmed", "rejected"):
            assert can_transition(terminal, target) is False


# ---------------------------------------------------------------------------
# Confirmation validation (§8.1 self-confirm, §8.2 version/hash binding)
# ---------------------------------------------------------------------------


def test_validate_confirmation_ok_when_version_and_hash_match_and_distinct_user():
    from app.services.plan_mode_core import validate_confirmation

    requester = uuid4()
    confirmer = uuid4()
    check = validate_confirmation(
        status="awaiting_confirmation",
        stored_version=1,
        stored_hash="sha256:abc",
        requested_by_user_id=requester,
        submitted_version=1,
        submitted_hash="sha256:abc",
        confirming_user_id=confirmer,
    )
    assert check.ok is True
    assert check.error_code is None


def test_validate_confirmation_allows_same_real_user_to_confirm():
    from app.services.plan_mode_core import validate_confirmation

    same_user = uuid4()
    check = validate_confirmation(
        status="awaiting_confirmation",
        stored_version=1,
        stored_hash="sha256:abc",
        requested_by_user_id=same_user,
        submitted_version=1,
        submitted_hash="sha256:abc",
        confirming_user_id=same_user,
    )
    assert check.ok is True
    assert check.error_code is None


def test_validate_confirmation_version_mismatch_is_conflict():
    from app.services.plan_mode_core import validate_confirmation

    check = validate_confirmation(
        status="awaiting_confirmation",
        stored_version=2,
        stored_hash="sha256:abc",
        requested_by_user_id=uuid4(),
        submitted_version=1,
        submitted_hash="sha256:abc",
        confirming_user_id=uuid4(),
    )
    assert check.ok is False
    assert check.error_code == "version_mismatch"


def test_validate_confirmation_hash_mismatch_is_conflict():
    from app.services.plan_mode_core import validate_confirmation

    check = validate_confirmation(
        status="awaiting_confirmation",
        stored_version=1,
        stored_hash="sha256:abc",
        requested_by_user_id=uuid4(),
        submitted_version=1,
        submitted_hash="sha256:DIFFERENT",
        confirming_user_id=uuid4(),
    )
    assert check.ok is False
    assert check.error_code == "hash_mismatch"


def test_validate_confirmation_wrong_status_cannot_confirm():
    from app.services.plan_mode_core import validate_confirmation

    check = validate_confirmation(
        status="planning",
        stored_version=1,
        stored_hash="sha256:abc",
        requested_by_user_id=uuid4(),
        submitted_version=1,
        submitted_hash="sha256:abc",
        confirming_user_id=uuid4(),
    )
    assert check.ok is False
    assert check.error_code == "not_confirmable"


def test_validate_confirmation_requires_real_user():
    from app.services.plan_mode_core import validate_confirmation

    check = validate_confirmation(
        status="awaiting_confirmation",
        stored_version=1,
        stored_hash="sha256:abc",
        requested_by_user_id=uuid4(),
        submitted_version=1,
        submitted_hash="sha256:abc",
        confirming_user_id=None,  # §8.5 must be a real user action
    )
    assert check.ok is False
    assert check.error_code == "missing_confirming_user"


# ---------------------------------------------------------------------------
# Markdown rendering (§6.2)
# ---------------------------------------------------------------------------


def test_render_plan_markdown_frontmatter_mirrors_db_fields():
    from app.services.plan_mode_core import build_plan_skeleton, compute_plan_hash, render_plan_markdown

    plan_id = uuid4()
    agent_id = uuid4()
    plan = build_plan_skeleton(intent_type="autonomous_wake", title="Daily brief", original_request="r")
    plan["objective"] = "obj"
    plan_hash = compute_plan_hash(plan)

    md = render_plan_markdown(
        plan_id=plan_id,
        agent_id=agent_id,
        tenant_id=None,
        status="awaiting_confirmation",
        plan_version=1,
        plan_hash=plan_hash,
        intent_type="autonomous_wake",
        created_at="2026-05-29T09:00:00+00:00",
        plan_json=plan,
    )

    assert md.startswith("---\n")
    assert "schema: hive_plan.v1" in md
    assert f"plan_id: {plan_id}" in md
    assert f"agent_id: {agent_id}" in md
    assert "tenant_id: null" in md
    assert "status: awaiting_confirmation" in md
    assert "plan_version: 1" in md
    assert f"plan_hash: {plan_hash}" in md
    assert "intent_type: autonomous_wake" in md
    assert "confirmed_by: null" in md
    # Human-readable body must surface objective + title.
    assert "Daily brief" in md
    assert "obj" in md


def test_render_plan_markdown_body_lists_steps_and_success_criteria():
    from app.services.plan_mode_core import build_plan_skeleton, compute_plan_hash, render_plan_markdown

    plan = build_plan_skeleton(intent_type="in_session_execution", title="Research", original_request="r")
    plan["objective"] = "Investigate the sector."
    plan["steps"] = [{"order": 1, "description": "Gather sources", "expected_output": "list"}]
    plan["success_criteria"] = ["Report cites 10+ sources."]

    md = render_plan_markdown(
        plan_id=uuid4(),
        agent_id=uuid4(),
        tenant_id=uuid4(),
        status="awaiting_confirmation",
        plan_version=1,
        plan_hash=compute_plan_hash(plan),
        intent_type="in_session_execution",
        created_at="2026-05-29T09:00:00+00:00",
        plan_json=plan,
    )

    assert "Gather sources" in md
    assert "Report cites 10+ sources." in md


def test_normalize_plan_json_rewrites_internal_capabilities_and_drops_placeholder_effects():
    from app.services.plan_mode_core import normalize_plan_json_for_validation

    normalized = normalize_plan_json_for_validation(
        {
            "required_capabilities": ["web_search", "web_fetch", "file_write", "channel_message", "web_search"],
            "external_side_effects": [
                {},
                {"kind": "", "channel": "", "audience": ""},
                "外部动作",
                "Send weekly report notification",
                {"kind": "message", "channel": "wechat", "audience": "rocky243", "requires_confirmation": True},
            ],
        }
    )

    assert normalized["required_capabilities"] == [
        "Web 来源核验",
        "工作区文件生成",
        "用户通知",
    ]
    assert normalized["external_side_effects"] == [
        {"description": "Send weekly report notification", "requires_confirmation": True},
        {"kind": "message", "channel": "wechat", "audience": "rocky243", "requires_confirmation": True},
    ]


def test_render_plan_markdown_hides_machine_contract_fields_from_user_body():
    from app.services.plan_mode_core import build_plan_skeleton, compute_plan_hash, render_plan_markdown

    plan = build_plan_skeleton(intent_type="autonomous_wake", title="RWA weekly report", original_request="r")
    plan["objective"] = "Generate the RWA weekly report."
    plan["steps"] = [{"order": 1, "description": "Prepare the report.", "expected_output": "report.md"}]
    plan["success_criteria"] = ["Report is delivered."]
    plan["required_capabilities"] = ["web_search", "file_write", "channel_message"]
    plan["external_side_effects"] = [{}, "外部动作"]

    md = render_plan_markdown(
        plan_id=uuid4(),
        agent_id=uuid4(),
        tenant_id=uuid4(),
        status="awaiting_confirmation",
        plan_version=1,
        plan_hash=compute_plan_hash(plan),
        intent_type="autonomous_wake",
        created_at="2026-05-29T09:00:00+00:00",
        plan_json=plan,
    )

    assert "RWA weekly report" in md
    assert "Prepare the report." in md
    assert "Required capabilities" not in md
    assert "External side effects" not in md
    assert "web_search" not in md
    assert "file_write" not in md
    assert "channel_message" not in md
    assert "外部动作" not in md


def test_render_plan_markdown_is_parseable_frontmatter():
    """Frontmatter block must be a clean leading --- ... --- region."""
    from app.services.plan_mode_core import build_plan_skeleton, compute_plan_hash, render_plan_markdown

    plan = build_plan_skeleton(intent_type="delegation", title="t", original_request="r")
    plan["objective"] = "obj"
    md = render_plan_markdown(
        plan_id=uuid4(),
        agent_id=uuid4(),
        tenant_id=None,
        status="awaiting_confirmation",
        plan_version=3,
        plan_hash=compute_plan_hash(plan),
        intent_type="delegation",
        created_at="2026-05-29T09:00:00+00:00",
        plan_json=plan,
    )
    # Exactly two frontmatter fences, both at line starts.
    fences = [i for i, line in enumerate(md.splitlines()) if line == "---"]
    assert fences[0] == 0
    assert len(fences) >= 2


def test_intent_types_are_the_documented_set():
    from app.services.plan_mode_core import INTENT_TYPES

    assert set(INTENT_TYPES) == {
        "autonomous_wake",
        "in_session_execution",
        "delegation",
    }


def test_plan_statuses_are_the_documented_set():
    from app.services.plan_mode_core import PLAN_STATUSES

    assert set(PLAN_STATUSES) == {
        "draft",
        "planning",
        "planning_failed",
        "awaiting_confirmation",
        "confirmed",
        "rejected",
        "superseded",
        "expired",
    }


def test_json_module_import_guard():
    """Sanity: canonical json must be valid JSON (round-trips)."""
    from app.services.plan_mode_core import build_plan_skeleton, canonical_plan_json

    plan = build_plan_skeleton(intent_type="delegation", title="t", original_request="r")
    assert json.loads(canonical_plan_json(plan))["schema"] == "hive_plan.v1"
