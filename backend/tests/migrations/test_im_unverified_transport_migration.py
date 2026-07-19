from __future__ import annotations

import importlib.util
from pathlib import Path


_PATH = Path(__file__).parents[2] / "alembic" / "versions" / "im_unverified_transport_0719.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("im_unverified_transport_0719", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_unverified_transport_migration_shuts_down_every_proofless_wechat_connection():
    module = _load_module()
    source = _PATH.read_text(encoding="utf-8")

    assert module.revision == "im_unverified_transport_0719"
    assert module.down_revision == "im_user_verified_binding_0719"
    assert "cc.channel_type = 'wechat_personal'" in source
    assert "ep.binding_method = 'wechat_qr'" in source
    assert "ep.binding_verified_at IS NOT NULL" in source
    assert "is_connected = FALSE" in source
    assert "'connection_status', 'identity_rebind_required'" in source
    assert "'identity_status', 'rebind_required'" in source
    assert "cc.self_identity_user_id IS NOT NULL" not in source
