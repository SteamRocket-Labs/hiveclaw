"""Tests for cut C1 (§12): agent-level definition store + scope resolution chain.

Scope model (docs/subagent-source-capability.md §12.1/§12.4/§12.5):
agent-level ``<workspace>/subagents/<name>.md`` wins over tenant-level
``_tenants/<tid>/subagents/definitions/<name>.md``; builtins are read-only
list rows; subagent memory follows the definition's scope (no cross-agent
memory bleed for agent-private definitions).
"""

from __future__ import annotations

import uuid

import pytest

from app.agents.subagent import SubagentSpec
from app.agents.subagent_definition import (
    SCOPE_AGENT,
    SCOPE_BUILTIN,
    SCOPE_TENANT,
    definition_store_for_agent,
    definition_store_for_tenant,
    list_subagent_definitions,
    resolve_subagent_definition,
)
from app.agents.subagent_memory import memory_store_for_agent, memory_store_for_tenant

AGENT_ID = uuid.UUID("00000000-0000-0000-0000-00000000a001")
TENANT_ID = uuid.UUID("00000000-0000-0000-0000-00000000b001")


def _spec(name: str, prompt: str) -> SubagentSpec:
    return SubagentSpec(name=name, description="d", type="explorer", system_prompt=prompt)


def test_agent_store_roundtrip(tmp_path):
    store = definition_store_for_agent(AGENT_ID, agent_data_dir=tmp_path)
    store.save(_spec("my-scout", "agent-level scout"))

    loaded = store.load("my-scout")
    assert loaded is not None
    assert loaded.system_prompt == "agent-level scout"
    # Path contract: lives under <root>/<agent_id>/subagents/
    assert (tmp_path / str(AGENT_ID) / "subagents" / "my-scout.md").exists()


def test_resolve_agent_scope_wins_over_tenant(tmp_path):
    definition_store_for_tenant(TENANT_ID, agent_data_dir=tmp_path).save(_spec("dup", "tenant version"))
    definition_store_for_agent(AGENT_ID, agent_data_dir=tmp_path).save(_spec("dup", "agent version"))

    resolved = resolve_subagent_definition("dup", agent_id=AGENT_ID, tenant_id=TENANT_ID, agent_data_dir=tmp_path)
    assert resolved is not None
    assert resolved.scope == SCOPE_AGENT
    assert resolved.spec.system_prompt == "agent version"


def test_resolve_falls_back_to_tenant(tmp_path):
    definition_store_for_tenant(TENANT_ID, agent_data_dir=tmp_path).save(_spec("shared", "tenant shared"))

    resolved = resolve_subagent_definition("shared", agent_id=AGENT_ID, tenant_id=TENANT_ID, agent_data_dir=tmp_path)
    assert resolved is not None
    assert resolved.scope == SCOPE_TENANT
    assert resolved.spec.system_prompt == "tenant shared"


def test_resolve_none_when_absent(tmp_path):
    assert resolve_subagent_definition("ghost", agent_id=AGENT_ID, tenant_id=TENANT_ID, agent_data_dir=tmp_path) is None


def test_delete_agent_definition_falls_back(tmp_path):
    definition_store_for_tenant(TENANT_ID, agent_data_dir=tmp_path).save(_spec("dup", "tenant version"))
    agent_store = definition_store_for_agent(AGENT_ID, agent_data_dir=tmp_path)
    agent_store.save(_spec("dup", "agent version"))

    (tmp_path / str(AGENT_ID) / "subagents" / "dup.md").unlink()

    resolved = resolve_subagent_definition("dup", agent_id=AGENT_ID, tenant_id=TENANT_ID, agent_data_dir=tmp_path)
    assert resolved is not None
    assert resolved.scope == SCOPE_TENANT
    assert resolved.spec.system_prompt == "tenant version"


def test_legacy_worker_definition_type_canonicalizes_on_read(tmp_path):
    store = definition_store_for_agent(AGENT_ID, agent_data_dir=tmp_path)
    path = tmp_path / str(AGENT_ID) / "subagents" / "legacy-writer.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nname: legacy-writer\ndescription: d\ntype: worker\n---\nlegacy prompt\n",
        encoding="utf-8",
    )

    loaded = store.load("legacy-writer")

    assert loaded is not None
    assert loaded.type == "general-purpose"


def test_list_merges_and_marks_scope(tmp_path):
    definition_store_for_tenant(TENANT_ID, agent_data_dir=tmp_path).save(_spec("dup", "tenant version"))
    definition_store_for_tenant(TENANT_ID, agent_data_dir=tmp_path).save(_spec("tenant-only", "tenant only"))
    definition_store_for_agent(AGENT_ID, agent_data_dir=tmp_path).save(_spec("dup", "agent version"))
    definition_store_for_agent(AGENT_ID, agent_data_dir=tmp_path).save(_spec("agent-only", "agent only"))

    rows = list_subagent_definitions(agent_id=AGENT_ID, tenant_id=TENANT_ID, agent_data_dir=tmp_path)
    by_name = {row["name"]: row for row in rows}

    # Same-name: agent scope wins, single row.
    assert by_name["dup"]["scope"] == SCOPE_AGENT
    assert sum(1 for row in rows if row["name"] == "dup") == 1
    assert by_name["agent-only"]["scope"] == SCOPE_AGENT
    assert by_name["tenant-only"]["scope"] == SCOPE_TENANT

    # Builtin types present as read-only template rows, never shadowing customs.
    for builtin in ("general-purpose", "explorer", "critic"):
        assert by_name[builtin]["scope"] == SCOPE_BUILTIN
        assert by_name[builtin]["type"] == builtin
    assert "worker" not in by_name
    # Custom definition named like a builtin would shadow it (agent/tenant wins).
    definition_store_for_agent(AGENT_ID, agent_data_dir=tmp_path).save(_spec("explorer", "custom explorer"))
    rows2 = list_subagent_definitions(agent_id=AGENT_ID, tenant_id=TENANT_ID, agent_data_dir=tmp_path)
    explorer_rows = [row for row in rows2 if row["name"] == "explorer"]
    assert len(explorer_rows) == 1
    assert explorer_rows[0]["scope"] == SCOPE_AGENT


def test_list_skips_filename_frontmatter_name_mismatch(tmp_path):
    agent_base = tmp_path / str(AGENT_ID) / "subagents"
    agent_base.mkdir(parents=True)
    (agent_base / "foo.md").write_text("---\nname: bar\ndescription: d\ntype: explorer\n---\nbody\n", encoding="utf-8")

    rows = list_subagent_definitions(agent_id=AGENT_ID, tenant_id=None, agent_data_dir=tmp_path)
    assert all(row["name"] != "bar" for row in rows)
    with pytest.raises(ValueError, match="mismatches file name"):
        resolve_subagent_definition("foo", agent_id=AGENT_ID, tenant_id=None, agent_data_dir=tmp_path)


def test_agent_memory_store_isolated_from_tenant(tmp_path):
    other_agent = uuid.UUID("00000000-0000-0000-0000-00000000a002")

    agent_mem = memory_store_for_agent(AGENT_ID, agent_data_dir=tmp_path)
    other_mem = memory_store_for_agent(other_agent, agent_data_dir=tmp_path)
    tenant_mem = memory_store_for_tenant(TENANT_ID, agent_data_dir=tmp_path)

    result = agent_mem.record_how("dup", "prefer primary sources for market sizing", category="source_calibration")
    assert result.written

    # Memory follows the definition's scope: nothing bleeds across stores.
    assert "primary sources" in agent_mem.load("dup")
    assert other_mem.load("dup") == ""
    assert tenant_mem.load("dup") == ""
    # Path contract: dot-dir keeps memory files out of the definition glob.
    assert (tmp_path / str(AGENT_ID) / "subagents" / ".memory" / "dup.memory.md").exists()
    assert definition_store_for_agent(AGENT_ID, agent_data_dir=tmp_path).list_names() == []


def test_agent_store_path_boundary(tmp_path):
    store = definition_store_for_agent(AGENT_ID, agent_data_dir=tmp_path)
    for evil in ("../escape", "a/b", "..", "x\\y"):
        with pytest.raises(ValueError, match="invalid subagent name"):
            store.load(evil)
