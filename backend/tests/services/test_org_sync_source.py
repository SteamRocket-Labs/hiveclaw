from pathlib import Path


def test_org_sync_service_matches_org_members_by_provider_backed_ids():
    source = Path("/Users/rocky243/vc-saas/hiveclaw/backend/app/services/org_sync_service.py").read_text(
        encoding="utf-8"
    )

    assert "OrgMember.external_id == user_id" in source
    assert "OrgMember.open_id == open_id" in source


def test_org_sync_service_reuses_auth_provider_identity_resolution_for_platform_users():
    source = Path("/Users/rocky243/vc-saas/hiveclaw/backend/app/services/org_sync_service.py").read_text(
        encoding="utf-8"
    )

    assert "feishu_auth_provider._find_user_by_external_identity" in source
    assert "feishu_auth_provider._find_user_by_legacy_fields" in source
    assert "feishu_auth_provider._create_user" in source
