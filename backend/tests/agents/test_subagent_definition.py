"""Tests for cut ⑤: persistent subagent 定义.md parse / render / store + system_prompt wiring."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.agents.subagent import (
    SUBAGENT_TYPE_CRITIC,
    SubagentJob,
    SubagentSpawnContext,
    SubagentSpec,
    _spawn_one,
    spawn_subagent_from_definition,
)
from app.agents.subagent_definition import (
    SubagentDefinitionStore,
    parse_subagent_definition,
    render_subagent_definition,
)


def test_parse_frontmatter_and_body():
    text = """---
name: market-explorer
description: Market research scout. Use for DeFi/market landscape questions.
type: explorer
allowed_tools:
  - web_search
  - read_file
max_tool_rounds: 6
memory: project
---

You are a market research explorer. Investigate and report.
"""
    spec = parse_subagent_definition(text)
    assert spec.name == "market-explorer"
    assert spec.description == "Market research scout. Use for DeFi/market landscape questions."
    assert spec.type == "explorer"
    assert spec.allowed_tools == ("web_search", "read_file")
    assert spec.max_tool_rounds == 6
    assert spec.memory_scope == "project"
    assert "market research explorer" in spec.system_prompt


def test_parse_requires_name():
    with pytest.raises(ValueError, match="name"):
        parse_subagent_definition("---\ntype: explorer\n---\nbody")


def test_parse_requires_description():
    # CC parity (parseAgentFromMarkdown): 'description' is the whenToUse the
    # parent model selects on — a definition without it is unusable.
    with pytest.raises(ValueError, match="description"):
        parse_subagent_definition("---\nname: scout\ntype: explorer\n---\nbody")
    with pytest.raises(ValueError, match="description"):
        parse_subagent_definition("---\nname: scout\ndescription: ''\ntype: explorer\n---\nbody")


@pytest.mark.parametrize(
    ("definition", "message"),
    [
        ("---\nname: bad\ndescription: d\ntype: explorer\nallowed_tools: read_file\n---\nbody", "allowed_tools"),
        ("---\nname: bad\ndescription: d\ntype: explorer\nexcluded_tools: write_file\n---\nbody", "excluded_tools"),
        ("---\nname: bad\ndescription: d\ntype: explorer\nallowed_tools:\n  - ''\n---\nbody", "allowed_tools"),
        ("---\nname: bad\ndescription: d\ntype: explorer\nmax_tool_rounds: nope\n---\nbody", "max_tool_rounds"),
        ("---\nname: bad\ndescription: d\ntype: explorer\nmax_tool_rounds: 0\n---\nbody", "max_tool_rounds"),
        ("---\nname: bad\ndescription: d\ntype: explorer\nisolation: session\n---\nbody", "isolation"),
        ("---\nname: bad\ndescription: d\ntype: explorer\nmemory: org\n---\nbody", "memory"),
    ],
)
def test_parse_rejects_invalid_frontmatter_contract_fields(definition, message):
    with pytest.raises(ValueError, match=message):
        parse_subagent_definition(definition)


def test_render_round_trips():
    spec = SubagentSpec(
        name="code-critic",
        description="Adversarial diff reviewer. Use before merging risky changes.",
        type=SUBAGENT_TYPE_CRITIC,
        allowed_tools=("read_file", "grep_search"),
        max_tool_rounds=4,
        memory_scope="local",
        system_prompt="Review the diff. Only verify, never modify.",
    )
    reparsed = parse_subagent_definition(render_subagent_definition(spec))
    assert reparsed.name == spec.name
    assert reparsed.description == spec.description
    assert reparsed.type == spec.type
    assert reparsed.allowed_tools == spec.allowed_tools
    assert reparsed.max_tool_rounds == spec.max_tool_rounds
    assert reparsed.memory_scope == "local"
    assert reparsed.system_prompt == spec.system_prompt


def test_render_rejects_empty_description():
    # Write-side mirror of the parse guard: anything rendered must read back.
    with pytest.raises(ValueError, match="description"):
        render_subagent_definition(SubagentSpec(name="scout", system_prompt="x"))


def test_store_save_load_roundtrip(tmp_path):
    store = SubagentDefinitionStore(tmp_path)
    store.save(SubagentSpec(name="scout", description="d", type="explorer", system_prompt="Scout ahead."))
    loaded = store.load("scout")
    assert loaded is not None
    assert loaded.name == "scout"
    assert loaded.system_prompt == "Scout ahead."


def test_store_load_missing_returns_none(tmp_path):
    assert SubagentDefinitionStore(tmp_path).load("nope") is None


def test_store_list_names(tmp_path):
    store = SubagentDefinitionStore(tmp_path)
    store.save(SubagentSpec(name="a", description="d"))
    store.save(SubagentSpec(name="b", description="d"))
    assert store.list_names() == ["a", "b"]


def test_store_tenant_isolation(tmp_path):
    # different base_dir = different tenant; no cross-tenant leakage
    t1 = SubagentDefinitionStore(tmp_path / "tenant1")
    t2 = SubagentDefinitionStore(tmp_path / "tenant2")
    t1.save(SubagentSpec(name="shared-name", description="d", system_prompt="t1"))
    assert t2.load("shared-name") is None
    loaded = t1.load("shared-name")
    assert loaded is not None and loaded.system_prompt == "t1"


def test_store_rejects_path_traversal_names(tmp_path):
    store = SubagentDefinitionStore(tmp_path)

    with pytest.raises(ValueError, match="subagent name"):
        store.save(SubagentSpec(name="../escape", description="d", system_prompt="bad"))

    with pytest.raises(ValueError, match="subagent name"):
        store.load("../escape")

    assert not (tmp_path.parent / "escape.md").exists()


def test_store_rejects_filename_frontmatter_name_mismatch(tmp_path):
    store = SubagentDefinitionStore(tmp_path)
    (tmp_path / "foo.md").write_text("---\nname: bar\ndescription: d\ntype: explorer\n---\nbody\n", encoding="utf-8")

    with pytest.raises(ValueError, match="mismatches file name"):
        store.load("foo")


@pytest.mark.asyncio
async def test_system_prompt_threads_to_request():
    captured: list = []

    async def invoke(request):
        captured.append(request)
        return SimpleNamespace(content="ok", tokens_used=1)

    ctx = SubagentSpawnContext(parent_agent_id=uuid.uuid4(), parent_user_id=uuid.uuid4(), model=SimpleNamespace())
    spec = SubagentSpec(name="s", type="explorer", system_prompt="CUSTOM PROMPT")
    await _spawn_one(ctx, SubagentJob(spec=spec, task="t"), invoke=invoke)
    # CC semantics: the definition body IS the whole system prompt (standalone),
    # and the layered suffix stays empty for subagent spawns.
    assert captured[0].standalone_system_prompt == "CUSTOM PROMPT"
    assert captured[0].system_prompt_suffix == ""


@pytest.mark.asyncio
async def test_spawn_from_persistent_definition_uses_stored_contract(tmp_path):
    captured: list = []
    store = SubagentDefinitionStore(tmp_path)
    store.save(
        SubagentSpec(
            name="scout",
            description="Stored critic contract for diff review.",
            type="critic",
            allowed_tools=("read_file",),
            max_tool_rounds=3,
            memory_scope="user",
            system_prompt="Use the stored critic contract.",
        )
    )

    async def invoke(request):
        captured.append(request)
        return SimpleNamespace(content="ok", tokens_used=1)

    ctx = SubagentSpawnContext(parent_agent_id=uuid.uuid4(), parent_user_id=uuid.uuid4(), model=SimpleNamespace())
    handle = await spawn_subagent_from_definition(ctx, store, "scout", "review this", invoke=invoke)

    assert handle.result is not None and handle.result.ok
    assert captured[0].standalone_system_prompt == "Use the stored critic contract."
    assert captured[0].allowed_tool_names == ("read_file",)
    assert captured[0].max_tool_rounds == 3
