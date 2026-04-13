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
