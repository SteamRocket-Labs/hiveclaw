from __future__ import annotations

import inspect


def _source(module_name: str) -> str:
    module = __import__(module_name, fromlist=["*"])
    return inspect.getsource(module)


def test_native_memory_soul_and_skill_writers_share_agent_asset_transaction() -> None:
    explicit = _source("app.memory.explicit_overlay")
    t3_gate = _source("app.memory.t3_platform_gate")
    dream = _source("app.services.auto_dream")
    registry = _source("app.services.skill_evolution_registry")
    lifecycle = _source("app.services.skill_lifecycle")
    installation = _source("app.services.skill_installation")

    assert "AgentAssetTransaction" in explicit
    assert "AgentAssetTransaction" in t3_gate
    assert "AgentAssetTransaction" in dream
    assert "AgentAssetTransaction" in registry
    assert "AgentAssetTransaction" in lifecycle
    assert "AgentAssetTransaction" in installation
    assert ".dream_writeback.lock" not in dream
    assert "_atomic_write_targets" not in t3_gate


def test_asset_transaction_uses_one_lock_revision_and_recovery_journal() -> None:
    transaction = _source("app.services.agent_asset_transaction")

    assert 'control / ".asset.lock"' in transaction
    assert 'control_root(agent_root) / "revision.json"' in transaction
    assert 'status"] = "prepared"' in transaction
    assert 'status"] = "applying"' in transaction
    assert 'status"] = "committed"' in transaction
    assert "recover_agent_asset_transactions" in transaction
    assert "compensate_agent_asset_transaction" in transaction
