from __future__ import annotations

from pathlib import Path
from uuid import uuid4


def test_external_visible_tool_preflight_still_requires_checkpoint() -> None:
    from app.services.action_preflight import ActionPreflightService, PreflightDecision
    from app.tools.service import _build_tool_preflight_input

    preflight = ActionPreflightService().evaluate(
        _build_tool_preflight_input(
            "send_feishu_message",
            {"message": "Send an external vendor update."},
        )
    )

    assert preflight.decision == PreflightDecision.ASK
    assert preflight.requires_checkpoint is True


def test_external_visible_tool_uses_runtime_owner_full_authority_policy() -> None:
    from app.services.action_preflight import CharterZone, ActionPreflightService, PreflightDecision
    from app.services.owner_action_policy import (
        ACTION_EXTERNAL_EFFECT,
        ACTION_LOCAL_READ,
        ACTION_LOCAL_WRITE,
        build_owner_action_policy,
    )
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import _build_tool_preflight_input

    agent_id = uuid4()
    tenant_id = uuid4()
    context = ToolExecutionContext(
        agent_id=agent_id,
        user_id=uuid4(),
        tenant_id=str(tenant_id),
        workspace=Path("/tmp/owner-action-policy"),
        owner_action_policy=build_owner_action_policy(
            agent_id=agent_id,
            tenant_id=tenant_id,
            actions={
                ACTION_EXTERNAL_EFFECT: CharterZone.FULL_AUTHORITY,
                ACTION_LOCAL_READ: CharterZone.FULL_AUTHORITY,
                ACTION_LOCAL_WRITE: CharterZone.FULL_AUTHORITY,
            },
            version=3,
            content_hash="policy-v3",
        ),
    )

    request = _build_tool_preflight_input(
        "send_feishu_message",
        {"message": "Send an external vendor update."},
        runtime_context=context,
    )
    preflight = ActionPreflightService().evaluate(request)

    assert request.action == ACTION_EXTERNAL_EFFECT
    assert request.charter_zone == CharterZone.FULL_AUTHORITY
    assert preflight.decision == PreflightDecision.DO
    assert preflight.requires_checkpoint is False


def test_external_visible_tool_uses_runtime_owner_never_do_policy() -> None:
    from app.services.action_preflight import CharterZone, ActionPreflightService, PreflightDecision
    from app.services.owner_action_policy import (
        ACTION_EXTERNAL_EFFECT,
        ACTION_LOCAL_READ,
        ACTION_LOCAL_WRITE,
        build_owner_action_policy,
    )
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import _build_tool_preflight_input

    agent_id = uuid4()
    tenant_id = uuid4()
    context = ToolExecutionContext(
        agent_id=agent_id,
        user_id=uuid4(),
        tenant_id=str(tenant_id),
        workspace=Path("/tmp/owner-action-policy"),
        owner_action_policy=build_owner_action_policy(
            agent_id=agent_id,
            tenant_id=tenant_id,
            actions={
                ACTION_EXTERNAL_EFFECT: CharterZone.NEVER_DO,
                ACTION_LOCAL_READ: CharterZone.FULL_AUTHORITY,
                ACTION_LOCAL_WRITE: CharterZone.FULL_AUTHORITY,
            },
            version=4,
            content_hash="policy-v4",
        ),
    )

    request = _build_tool_preflight_input(
        "send_feishu_message",
        {"message": "Send an external vendor update."},
        runtime_context=context,
    )
    preflight = ActionPreflightService().evaluate(request)

    assert request.action == ACTION_EXTERNAL_EFFECT
    assert request.charter_zone == CharterZone.NEVER_DO
    assert preflight.decision == PreflightDecision.REFUSE
    assert preflight.reasons == ["charter_never_do"]


def test_unavailable_owner_policy_keeps_read_only_tool_non_effectful() -> None:
    from app.services.action_preflight import ActionPreflightService, CharterZone, PreflightDecision
    from app.services.owner_action_policy import build_unavailable_owner_action_policy
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import _build_tool_preflight_input

    agent_id = uuid4()
    tenant_id = uuid4()
    context = ToolExecutionContext(
        agent_id=agent_id,
        user_id=uuid4(),
        tenant_id=str(tenant_id),
        workspace=Path("/tmp/owner-action-policy"),
        owner_action_policy=build_unavailable_owner_action_policy(
            agent_id=agent_id,
            tenant_id=tenant_id,
            error_code="policy_dependency_unavailable",
        ),
    )

    request = _build_tool_preflight_input(
        "read_file",
        {"path": "notes.md"},
        runtime_context=context,
    )

    assert request.action == "tool.local_read"
    assert request.action_effectful is False
    assert request.charter_zone == CharterZone.FULL_AUTHORITY
    assert request.charter_policy_valid is False
    assert ActionPreflightService().evaluate(request).decision == PreflightDecision.DO


def test_sensitive_tool_preflight_still_refuses_credentials() -> None:
    from app.services.action_preflight import ActionPreflightService, PreflightDecision
    from app.tools.service import _build_tool_preflight_input

    preflight = ActionPreflightService().evaluate(
        _build_tool_preflight_input(
            "write_file",
            {"path": "secrets.txt", "content": "api_key=sk-1234567890abcdefghijklmnop"},
        )
    )

    assert preflight.decision == PreflightDecision.REFUSE


def test_local_tool_arguments_are_not_semantically_classified_as_company_conflict() -> None:
    from app.services.action_preflight import ActionPreflightService, PreflightDecision
    from app.tools.service import _build_tool_preflight_input

    request = _build_tool_preflight_input(
        "write_file",
        {
            "path": "policy-review.md",
            "content": "Explain why agents must never bypass company policy.",
        },
    )
    preflight = ActionPreflightService().evaluate(request)

    assert request.company_boundary_conflict is False
    assert preflight.decision == PreflightDecision.DO
