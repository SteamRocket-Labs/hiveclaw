"""Report the accepted per-entry work of non-batched team-memory reads."""

import json
import os
from pathlib import Path
import sys

import pytest

from tests.integration._a2w_memory_phases import memory_phase_profile
from tests.integration.test_a2w_review_boundaries_postgres import _context
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import shared_memory  # noqa: F401
from tests.integration.test_m4d_group_memory_postgres import group_memory  # noqa: F401
from tests.integration.test_m4e_team_memory_authority_postgres import runtime_team  # noqa: F401
from tests.integration.test_m4f_team_memory_runtime_postgres import readable_team_memory  # noqa: F401
from tests.integration.test_r15e_artifact_graph_reads_postgres import _profile

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("entry_count", [1, 2])
async def test_team_memory_keeps_independent_live_work(
    readable_team_memory, runtime_team, monkeypatch, entry_count,
):
    from app.tools.handlers.memory import _search_memory, search_memory

    shared, workspace, _receipt = readable_team_memory
    n = shared.n
    if entry_count == 2:
        saved = await runtime_team[2][0].put("/enterprise/memory/shared", json={"workspace_key": workspace,
            "key": "second", "title": "Second release", "content": "SECOND_TEAM_RELEASE_MARKER", "mode": "replace"})
        assert saved.status_code == 200, saved.text
    arguments = {"query": "release", "scope": "all"}
    context = _context(n)
    reference = await _search_memory(n.agent, arguments, str(n.tenant), execution_context=context)
    with _profile(monkeypatch) as totals:
        with memory_phase_profile(monkeypatch) as phases:
            result = await search_memory(n.agent, arguments, str(n.tenant), execution_context=context)
    assert result == reference
    assert "TEAM_PROJECT_RELEASE_MARKER" in result
    if entry_count == 2:
        assert "SECOND_TEAM_RELEASE_MARKER" in result
    row = {"team_entries": entry_count, "statements": sum(value["statements"] for value in totals.values()),
        "by_phase": [{"stage": stage, "kind": kind, "checkpoint": checkpoint, "phase": json.loads(phase), **value}
            for (stage, kind, checkpoint, phase), value in phases.items()]}
    print("A2W_TEAM_WORK", json.dumps(row), flush=True)
    if os.environ.get("FULL_RUN"):
        (Path(os.environ["FULL_RUN"]) / "evidence" / f"{sys.argv[1]}-team-{entry_count}.json").write_text(
            json.dumps(row, indent=2))
