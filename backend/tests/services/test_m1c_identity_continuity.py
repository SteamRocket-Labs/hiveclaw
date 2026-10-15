"""Red cases for Feishu sync and authenticated identity continuity.

These assertions describe the required subject rules. They fail while email
fallback, a shared sync password, or a single matching kind can still hide a
different account.
"""

from __future__ import annotations

import inspect

from app.services.auth_provider import FeishuAuthProvider
from app.services.channel_user_service import ChannelUserService
from app.services.org_sync_service import OrgSyncService


def test_org_sync_does_not_provision_the_shared_password() -> None:
    source = inspect.getsource(OrgSyncService.full_sync)
    assert "123456" not in source


def test_org_sync_does_not_match_platform_users_by_email() -> None:
    source = inspect.getsource(OrgSyncService)
    assert "User.email" not in source


def test_legacy_oauth_recognition_does_not_match_email() -> None:
    source = inspect.getsource(FeishuAuthProvider._find_user_by_legacy_fields)
    assert "User.email" not in source


def test_inbound_resolution_does_not_fall_back_to_email() -> None:
    source = inspect.getsource(ChannelUserService.resolve_feishu_user)
    assert "_find_by_email" not in source
    assert "User.email" not in source
