"""G3f Round 2 item 1: historical snapshots cannot grant system identity."""

from types import SimpleNamespace

import pytest

from app.services.ai_asset_adapters import apply_agent_content


@pytest.mark.parametrize("current,changes", [
    ("internal_tenant", {"agent_class": "internal_system", "name": "__system_hr__"}),
    ("internal_system", {"name": "Renamed HR"}),
    ("internal_system", {"agent_class": "internal_tenant"}),
])
def test_g3f_round2_item1_asset_snapshot_cannot_change_system_identity(current, changes):
    agent = SimpleNamespace(agent_class=current, name="__system_hr__" if current == "internal_system" else "Research assistant")
    original = vars(agent).copy()
    with pytest.raises(ValueError, match="system HR"):
        apply_agent_content(agent, {"asset_type": "agent", "config": changes})
    assert vars(agent) == original


def test_g3f_round2_item1_hr_snapshot_can_restore_other_settings_with_same_identity():
    agent = SimpleNamespace(agent_class="internal_system", name="__system_hr__", role_description="Before")
    apply_agent_content(agent, {"asset_type": "agent", "config": {
        "agent_class": "internal_system", "name": "__system_hr__", "role_description": "After"}})
    assert agent.role_description == "After" and agent.agent_class == "internal_system"
