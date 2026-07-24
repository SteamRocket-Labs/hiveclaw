from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("relative_path", "retired_fragment"),
    [
        ("app/services/web_chat_runtime.py", "async def handle_web_chat_disconnect"),
        ("app/services/heartbeat.py", "async def start_heartbeat"),
        ("app/api/websocket.py", "class ConnectionManager"),
        ("app/api/websocket.py", "async def _claim_pending_reply_suffix_for_session"),
        ("app/api/websocket.py", '@router.get("/chat/{agent_id}/history")'),
        ("app/kernel/engine.py", "def _parse_interactive_plan_signal"),
        ("app/kernel/engine.py", "def _maybe_activate_interactive_plan_from_tool_result"),
        ("app/kernel/engine.py", "def _is_live_interactive_chat"),
    ],
)
def test_sa09_retired_runtime_shims_are_absent(relative_path: str, retired_fragment: str) -> None:
    source = (BACKEND_ROOT / relative_path).read_text(encoding="utf-8")

    assert retired_fragment not in source


def test_sa09_llm_utils_reexport_shim_is_absent() -> None:
    assert not (BACKEND_ROOT / "app/services/llm_utils.py").exists()


def test_sa09b_database_work_ledger_authority_is_absent() -> None:
    assert not (BACKEND_ROOT / "app/models/work_ledger.py").exists()

    bootstrap = (BACKEND_ROOT / "app/db_bootstrap.py").read_text(encoding="utf-8")
    assert '"agent_work_ledgers"' not in bootstrap


def test_a2a04_in_process_sentinel_runtime_is_absent() -> None:
    coordination = (BACKEND_ROOT / "app/agents/coordination.py").read_text(encoding="utf-8")
    repository = (BACKEND_ROOT / "app/agents/coordination_repository.py").read_text(encoding="utf-8")
    handbook = (BACKEND_ROOT.parent / "AGENTS.md").read_text(encoding="utf-8")

    for fragment in (
        "SentinelRuntimePath",
        "class Sentinel:",
        "class SentinelEmission:",
        "self._sentinels",
        "def register_sentinel",
        "def fire_sentinel",
    ):
        assert fragment not in coordination
    assert "Sentinel-emitted variants" not in repository
    assert "Sentinel state stays in-process" not in repository
    assert "unconsumed in-process Sentinel helper" not in handbook


def test_a2a04b_legacy_agent_relationship_authority_is_absent() -> None:
    org_models = (BACKEND_ROOT / "app/models/org.py").read_text(encoding="utf-8")
    seed = (BACKEND_ROOT / "seed.py").read_text(encoding="utf-8")
    bootstrap = (BACKEND_ROOT / "app/db_bootstrap.py").read_text(encoding="utf-8")
    stage2b_backfill = (BACKEND_ROOT / "app/scripts/backfill_stage2b_tenant_id.py").read_text(encoding="utf-8")

    assert "class AgentAgentRelationship" not in org_models
    assert "AgentAgentRelationship" not in seed
    assert '"agent_agent_relationships"' not in bootstrap
    assert '"agent_agent_relationships"' not in stage2b_backfill
