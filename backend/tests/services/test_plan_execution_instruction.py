"""E: the confirmed-plan marching-orders builder.

``build_plan_execution_instruction`` is the single source the live-continuation
handoff and the scheduled-trigger wake both render from, so the "execute this
confirmed plan" wording cannot drift between the two paths.
"""

from __future__ import annotations

from app.services.plan_mode_core import build_plan_execution_instruction


def test_instruction_carries_plan_markdown_body_and_provenance():
    text = build_plan_execution_instruction(
        plan_id="plan-1",
        plan_version=3,
        plan_markdown="## Step 1\nDo the thing.",
        objective="obj",
        original_request="orig",
        source="live",
    )
    assert "plan-1" in text  # provenance
    assert "version=3" in text
    assert "Do the thing." in text  # the authored body is the substance
    assert "# 已确认的计划" in text


def test_instruction_falls_back_to_objective_then_original_request():
    # No authored markdown -> the objective is the fallback body.
    t1 = build_plan_execution_instruction(
        plan_id="p", plan_version=1, plan_markdown="", objective="the objective", original_request="orig"
    )
    assert "the objective" in t1
    # No markdown and no objective -> original_request is the last resort.
    t2 = build_plan_execution_instruction(
        plan_id="p", plan_version=1, plan_markdown="", objective="", original_request="the original request"
    )
    assert "the original request" in t2


def test_trigger_and_live_sources_differ_in_delivery_clause():
    live = build_plan_execution_instruction(plan_id="p", plan_version=1, plan_markdown="body", source="live")
    trig = build_plan_execution_instruction(plan_id="p", plan_version=1, plan_markdown="body", source="trigger")
    # Both execute the SAME confirmed plan; only the delivery clause differs
    # (live chat continuation vs a scheduled autonomous wake).
    assert "当前这个对话" in live
    assert "当前这个对话" not in trig
    assert "定时唤醒" in trig
    # Neither path re-enters Plan Mode or re-asks for confirmation.
    assert "不要重新进入计划模式" in live
    assert "不要重新进入计划模式" in trig
