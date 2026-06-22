from __future__ import annotations

import os
import stat

from hive_bridge.token_store import BridgeConfig, FileTokenStore


def test_file_token_store_writes_0600_config(tmp_path) -> None:
    store = FileTokenStore(config_path=tmp_path / "config.json")

    store.save(
        BridgeConfig(
            base_url="https://hive.example",
            token="hb_secret",
            connection_id="conn-1",
            agent_id="agent-1",
            tenant_id="tenant-1",
        )
    )

    loaded = store.load()
    assert loaded is not None
    assert loaded.token == "hb_secret"
    mode = stat.S_IMODE(os.stat(tmp_path / "config.json").st_mode)
    assert mode == 0o600


def test_file_token_store_missing_returns_none(tmp_path) -> None:
    assert FileTokenStore(config_path=tmp_path / "missing.json").load() is None
