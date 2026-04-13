from pathlib import Path


def test_maintenance_scripts_do_not_import_removed_schedule_model():
    project_root = Path("/Users/rocky243/vc-saas/hiveclaw")
    duplicate_feishu_script = (project_root / "backend/app/scripts/cleanup_duplicate_feishu_users.py").read_text(
        encoding="utf-8"
    )
    gateway_script = (project_root / "backend/app/scripts/cleanup_legacy_gateway_conversations.py").read_text(
        encoding="utf-8"
    )

    assert "plaza, schedule, skill" not in duplicate_feishu_script
    assert "plaza, schedule, skill" not in gateway_script


def test_gateway_maintenance_script_prefers_db_level_migration_helper():
    gateway_script = Path(
        "/Users/rocky243/vc-saas/hiveclaw/backend/app/scripts/cleanup_legacy_gateway_conversations.py"
    ).read_text(encoding="utf-8")

    assert "promote_legacy_gateway_conversations" in gateway_script
    assert "normalize_legacy_gateway_conversations" not in gateway_script


def test_feishu_maintenance_script_prefers_db_level_session_normalization():
    feishu_script = Path(
        "/Users/rocky243/vc-saas/hiveclaw/backend/app/scripts/cleanup_duplicate_feishu_users.py"
    ).read_text(encoding="utf-8")

    assert "promote_legacy_feishu_sessions" in feishu_script
    assert "reconcile_feishu_identity_state" not in feishu_script
