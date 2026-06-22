from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class BridgeConfig:
    base_url: str
    token: str
    connection_id: str | None = None
    agent_id: str | None = None
    tenant_id: str | None = None


def default_config_path() -> Path:
    return Path.home() / ".hive" / "bridge" / "config.json"


class FileTokenStore:
    """Simple 0600 JSON token store.

    P0 intentionally keeps this dependency-light. OS Keychain support can wrap
    this interface later without changing the CLI/client contract.
    """

    def __init__(self, *, config_path: Path | None = None) -> None:
        self.config_path = config_path or default_config_path()

    def load(self) -> BridgeConfig | None:
        if not self.config_path.exists():
            return None
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        return BridgeConfig(
            base_url=data["base_url"],
            token=data["token"],
            connection_id=data.get("connection_id"),
            agent_id=data.get("agent_id"),
            tenant_id=data.get("tenant_id"),
        )

    def save(self, config: BridgeConfig) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.config_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(asdict(config), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp_path, 0o600)
        tmp_path.replace(self.config_path)
        os.chmod(self.config_path, 0o600)

    def clear(self) -> None:
        if self.config_path.exists():
            self.config_path.unlink()
