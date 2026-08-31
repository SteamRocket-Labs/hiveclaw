from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_canonical_hive_connect_is_the_only_local_agent_client_surface() -> None:
    legacy_root = REPO_ROOT / "local_bridge"
    retired_entrypoints = (
        legacy_root / "package.json",
        legacy_root / "pyproject.toml",
        legacy_root / "bin" / "hive-bridge.mjs",
        legacy_root / "src" / "client.mjs",
        legacy_root / "hive_bridge" / "client.py",
        legacy_root / "hive_bridge" / "poller.py",
        legacy_root / "hive_bridge_auto_adapter.py",
        legacy_root / "skills" / "hive-bridge" / "SKILL.md",
        legacy_root / "skill-package" / "skills" / "hive-bridge" / "SKILL.md",
    )

    assert all(not path.exists() for path in retired_entrypoints)

    install_service = (REPO_ROOT / "backend" / "app" / "services" / "local_bridge_service.py").read_text(
        encoding="utf-8"
    )
    local_agents_page = (REPO_ROOT / "frontend" / "src" / "pages" / "LocalAgents.tsx").read_text(encoding="utf-8")
    assert "HIVE_CONNECT_SKILL_REPO_URL" in install_service
    assert "HIVE_CONNECT_NPM_PACKAGE" in install_service
    assert "skill_repo_url: ''" in local_agents_page
    assert "npm_package: ''" in local_agents_page
    assert "hive-bridge" not in install_service.lower()
    assert "hive-bridge" not in local_agents_page.lower()
