from __future__ import annotations

from pathlib import Path


def _python_sources() -> dict[str, str]:
    project_root = Path(__file__).resolve().parents[3]
    app_root = project_root / "backend/app"
    return {
        str(path.relative_to(project_root)): path.read_text(encoding="utf-8")
        for path in app_root.rglob("*.py")
    }


def test_legacy_session_compat_is_localized_to_allowlist() -> None:
    sources = _python_sources()

    gw_agent_refs = sorted(path for path, source in sources.items() if "gw_agent_" in source)
    feishu_lookup_refs = sorted(path for path, source in sources.items() if "build_feishu_session_lookup_ids" in source)
    feishu_session_maintenance_refs = sorted(
        path for path, source in sources.items() if "normalize_feishu_chat_sessions(" in source
    )
    feishu_reconcile_refs = sorted(
        path for path, source in sources.items() if "reconcile_feishu_identity_state(" in source
    )

    assert gw_agent_refs == [
        "backend/app/db_legacy_gateway_conversation_migration.py",
        "backend/app/scripts/cleanup_legacy_gateway_conversations.py",
        "backend/app/session_identifiers.py",
    ]
    assert feishu_lookup_refs == [
        "backend/app/services/feishu_identity_maintenance.py",
        "backend/app/session_identifiers.py",
    ]
    assert feishu_session_maintenance_refs == []
    assert feishu_reconcile_refs == []
