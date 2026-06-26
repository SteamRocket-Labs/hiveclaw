from __future__ import annotations

from types import SimpleNamespace


def test_session_tool_approvals_are_hidden_from_enterprise_surfaces() -> None:
    from app.services.enterprise_approval_visibility import is_session_tool_approval, is_visible_enterprise_approval

    accidental_session_tool = SimpleNamespace(
        action_type="external.web.search",
        details={
            "tool": "web_search",
            "args": {"query": "github trending"},
            "origin": {"type": "agent_session", "session_id": "session-1"},
        },
    )
    accidental_legacy_tool = SimpleNamespace(
        action_type="workspace.file.read",
        details={
            "tool": "fs_read",
            "args": {"path": "memory/source.md"},
            "origin": {"type": "approval_request", "session_id": None},
        },
    )
    enterprise_approval = SimpleNamespace(
        action_type="enterprise.asset.delete",
        details={"asset_type": "agent", "agent_id": "agent-1", "origin": {"type": "enterprise_admin"}},
    )

    assert is_session_tool_approval(accidental_session_tool) is True
    assert is_visible_enterprise_approval(accidental_session_tool) is False
    assert is_session_tool_approval(accidental_legacy_tool) is True
    assert is_visible_enterprise_approval(accidental_legacy_tool) is False
    assert is_session_tool_approval(enterprise_approval) is False
    assert is_visible_enterprise_approval(enterprise_approval) is True


def test_enterprise_visible_filter_excludes_session_tool_approvals() -> None:
    from sqlalchemy.dialects import postgresql

    from app.models.audit import ApprovalRequest
    from app.services.enterprise_approval_visibility import enterprise_visible_approval_filter

    compiled = str(
        enterprise_visible_approval_filter(ApprovalRequest).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "agent_session" in compiled
    assert "approval_request" in compiled
    assert "tool" in compiled
