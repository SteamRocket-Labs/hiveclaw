from __future__ import annotations

import importlib.util
from pathlib import Path


_PATH = Path(__file__).parents[2] / "alembic" / "versions" / "im_user_verified_binding_0719.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("im_user_verified_binding_0719", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_verified_binding_migration_is_the_single_head_and_never_infers_a_user():
    module = _load_module()
    source = _PATH.read_text(encoding="utf-8")

    assert module.revision == "im_user_verified_binding_0719"
    assert module.down_revision == "im_channel_identity_closure_0718"
    assert "binding_method" in source
    assert "binding_verified_at" in source
    assert "provider = 'wechat_personal' AND binding_method = 'wechat_qr'" in source
    assert "provider = 'feishu' AND binding_method = 'feishu_qr'" in source
    assert "channel_type IN ('wechat_personal','feishu')" in source
    assert "profile_json ->> 'identity_source' = 'authenticated_channel_connect'" in source
    assert "Migration removed an IM identity assignment without provider proof" in source
    assert "UPDATE chat_sessions" in source
    assert "UPDATE chat_messages" in source
    assert "'connection_status', 'identity_rebind_required'" in source
    assert "'identity_status', 'rebind_required'" in source
    assert "previous runtime has no Feishu QR-proof contract" in source
    assert "owner_user_id" not in source
    assert "creator_id" not in source
