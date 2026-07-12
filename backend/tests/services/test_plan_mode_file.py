from __future__ import annotations

from uuid import uuid4

import pytest


def test_provision_agent_plan_file_slot_creates_parent_without_unowned_placeholder(tmp_path) -> None:
    from app.services.plan_mode_file import provision_agent_plan_file_slot

    agent_id = uuid4()
    relative_path = "workspace/plans/draft.plan.md"

    absolute_path = provision_agent_plan_file_slot(
        agent_id,
        relative_path,
        agent_data_dir=tmp_path,
    )

    assert absolute_path == tmp_path / str(agent_id) / relative_path
    assert absolute_path.parent.is_dir()
    assert not absolute_path.exists()


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "/tmp/plan.md",
        "workspace/../outside.plan.md",
        "../outside.plan.md",
    ],
)
def test_provision_agent_plan_file_slot_rejects_paths_outside_agent_workspace(tmp_path, unsafe_path) -> None:
    from app.services.plan_mode_file import provision_agent_plan_file_slot

    with pytest.raises(ValueError, match="relative to the agent workspace"):
        provision_agent_plan_file_slot(
            uuid4(),
            unsafe_path,
            agent_data_dir=tmp_path,
        )
