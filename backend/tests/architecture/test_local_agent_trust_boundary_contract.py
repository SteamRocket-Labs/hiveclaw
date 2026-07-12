from __future__ import annotations

from pathlib import Path


DOC_PATH = Path(__file__).resolve().parents[3] / "docs" / "agent-native-atomic-source-audit-2026-07-12.md"


def test_local_agent_trust_boundary_is_explicit_and_does_not_overclaim_cloud_control() -> None:
    source = DOC_PATH.read_text(encoding="utf-8")
    assert "受信本地代理边界" in source
    assert "逐动作审批" in source
    assert "replay_key" in source
    assert "Hive 无法机械证明" in source
    assert "操作系统沙箱" in source
    assert "bearer token" in source
    assert "delivered reconciler" in source
