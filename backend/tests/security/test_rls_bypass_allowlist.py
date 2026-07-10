from __future__ import annotations

from collections import Counter
from datetime import date
from pathlib import Path

from app.core.rls_bypass_manifest import RLS_BYPASS_ALLOWLIST, scan_rls_bypass_callsites


APP_ROOT = Path(__file__).resolve().parents[2] / "app"


def test_every_rls_bypass_callsite_is_registered_with_exact_query_shape() -> None:
    actual = Counter(call.signature for call in scan_rls_bypass_callsites(APP_ROOT))
    registered = Counter(grant.signature for grant in RLS_BYPASS_ALLOWLIST)

    assert actual == registered


def test_every_rls_bypass_grant_has_owner_expiry_and_query_fields() -> None:
    for grant in RLS_BYPASS_ALLOWLIST:
        assert grant.owner.strip()
        assert grant.expires_on >= date.today()
        assert grant.allowed_query_fields
        assert grant.reason_expression.strip()


def test_bypass_scanner_rejects_missing_reason(tmp_path: Path) -> None:
    app_root = tmp_path / "app"
    app_root.mkdir()
    (app_root / "bad.py").write_text(
        "async def bad(db):\n    async with enter_rls_bypass(db):\n        return None\n",
        encoding="utf-8",
    )

    calls = scan_rls_bypass_callsites(app_root)

    assert len(calls) == 1
    assert calls[0].reason_expression == ""


def test_agent_runtime_bootstrap_has_no_business_row_bypass() -> None:
    callsites = scan_rls_bypass_callsites(APP_ROOT)
    forbidden_business_files = {
        "app/runtime/invoker.py",
        "app/services/agent_team_runtime_service.py",
        "app/services/command_escalation_service.py",
        "app/services/mcp_server_service.py",
        "app/services/quota_guard.py",
        "app/services/subagent_run_service.py",
        "app/services/runtime_task_fence.py",
        "app/services/web_chat_runtime.py",
        "app/tools/governance_resolver.py",
        "app/tools/handlers/subagent.py",
        "app/tools/resolver.py",
        "app/tools/workspace.py",
    }

    assert not [call for call in callsites if call.file in forbidden_business_files]


def test_workflow_runtime_uses_locator_then_tenant_scope() -> None:
    callsites = scan_rls_bypass_callsites(APP_ROOT)

    assert not [
        call
        for call in callsites
        if (
            call.file
            in {
                "app/services/workflow_launch.py",
                "app/services/workflow_signal_consumer.py",
            }
            or (
                call.file == "app/services/workflow_runtime_service.py"
                and call.function in {"_tenant_for_run", "resume_pending_runs"}
            )
        )
    ]


def test_runtime_control_business_reads_use_tenant_locators() -> None:
    callsites = scan_rls_bypass_callsites(APP_ROOT)

    assert not [
        call
        for call in callsites
        if call.file == "app/services/runtime_control_bus.py"
        and call.function in {"_load_session_lifecycle_messages", "bridge_transcript_event_to_t0"}
    ]
