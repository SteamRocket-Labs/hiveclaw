from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace


def test_build_feishu_p2p_conv_id_prefers_user_id():
    from app.services.feishu_identity_maintenance import build_feishu_p2p_conv_id

    assert build_feishu_p2p_conv_id("u_123", "ou_456") == "feishu_p2p_u_123"
    assert build_feishu_p2p_conv_id(None, "ou_456") == "feishu_p2p_ou_456"


def test_choose_canonical_feishu_user_prefers_stable_user_id_real_email_then_oldest():
    from app.services.feishu_identity_maintenance import choose_canonical_feishu_user

    older = datetime(2025, 1, 1, tzinfo=timezone.utc)
    newer = datetime(2025, 2, 1, tzinfo=timezone.utc)

    duplicate_with_fake_email = SimpleNamespace(
        id="dup-fake",
        email="dup@feishu.local",
        feishu_user_id="u_123",
        created_at=older,
    )
    canonical = SimpleNamespace(
        id="canonical",
        email="real@company.com",
        feishu_user_id="u_123",
        created_at=newer,
    )
    weak_candidate = SimpleNamespace(
        id="weak",
        email="weak@company.com",
        feishu_user_id=None,
        created_at=older,
    )

    picked = choose_canonical_feishu_user([duplicate_with_fake_email, canonical, weak_candidate])

    assert picked is canonical
