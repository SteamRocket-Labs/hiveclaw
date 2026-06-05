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
type: explorer
allowed_tools:
  - web_search
  - read_file
max_tool_rounds: 6
---

You are a market research explorer. Investigate and report.
"""
    spec = parse_subagent_definition(text)
    assert spec.name == "market-explorer"
    assert spec.type == "explorer"
    assert spec.allowed_tools == ("web_search", "read_file")
    assert spec.max_tool_rounds == 6
    assert "market research explorer" in spec.system_prompt


def test_parse_requires_name():
    with pytest.raises(ValueError, match="name"):
        parse_subagent_definition("---\ntype: explorer\n---\nbody")


@pytest.mark.parametrize(
    ("definition", "message"),
    [
        ("---\nname: bad\ntype: explorer\nallowed_tools: read_file\n---\nbody", "allowed_tools"),
        ("---\nname: bad\ntype: explorer\nexcluded_tools: write_file\n---\nbody", "excluded_tools"),
        ("---\nname: bad\ntype: explorer\nallowed_tools:\n  - ''\n---\nbody", "allowed_tools"),
        ("---\nname: bad\ntype: explorer\nmax_tool_rounds: nope\n---\nbody", "max_tool_rounds"),
        ("---\nname: bad\ntype: explorer\nmax_tool_rounds: 0\n---\nbody", "max_tool_rounds"),
        ("---\nname: bad\ntype: explorer\nisolation: session\n---\nbody", "isolation"),
    ],
)
def test_parse_rejects_invalid_frontmatter_contract_fields(definition, message):
    with pytest.raises(ValueError, match=message):
        parse_subagent_definition(definition)


def test_render_round_trips():
    spec = SubagentSpec(
        name="code-critic",
        type=SUBAGENT_TYPE_CRITIC,
        allowed_tools=("read_file", "grep_search"),
        max_tool_rounds=4,
        system_prompt="Review the diff. Only verify, never modify.",
    )
    reparsed = parse_subagent_definition(render_subagent_definition(spec))
    assert reparsed.name == spec.name
    assert reparsed.type == spec.type
    assert reparsed.allowed_tools == spec.allowed_tools
    assert reparsed.max_tool_rounds == spec.max_tool_rounds
    assert reparsed.system_prompt == spec.system_prompt


def test_store_save_load_roundtrip(tmp_path):
    store = SubagentDefinitionStore(tmp_path)
    store.save(SubagentSpec(name="scout", type="explorer", system_prompt="Scout ahead."))
    loaded = store.load("scout")
    assert loaded is not None
    assert loaded.name == "scout"
    assert loaded.system_prompt == "Scout ahead."


def test_store_load_missing_returns_none(tmp_path):
    assert SubagentDefinitionStore(tmp_path).load("nope") is None


def test_store_list_names(tmp_path):
    store = SubagentDefinitionStore(tmp_path)
    store.save(SubagentSpec(name="a"))
    store.save(SubagentSpec(name="b"))
    assert store.list_names() == ["a", "b"]


def test_store_tenant_isolation(tmp_path):
    # different base_dir = different tenant; no cross-tenant leakage
    t1 = SubagentDefinitionStore(tmp_path / "tenant1")
    t2 = SubagentDefinitionStore(tmp_path / "tenant2")
    t1.save(SubagentSpec(name="shared-name", system_prompt="t1"))
    assert t2.load("shared-name") is None
    loaded = t1.load("shared-name")
    assert loaded is not None and loaded.system_prompt == "t1"


def test_store_rejects_path_traversal_names(tmp_path):
    store = SubagentDefinitionStore(tmp_path)

    with pytest.raises(ValueError, match="subagent name"):
        store.save(SubagentSpec(name="../escape", system_prompt="bad"))

    with pytest.raises(ValueError, match="subagent name"):
        store.load("../escape")

    assert not (tmp_path.parent / "escape.md").exists()


def test_store_rejects_filename_frontmatter_name_mismatch(tmp_path):
    store = SubagentDefinitionStore(tmp_path)
    (tmp_path / "foo.md").write_text("---\nname: bar\ntype: explorer\n---\nbody\n", encoding="utf-8")

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
    assert captured[0].system_prompt_suffix == "CUSTOM PROMPT"


@pytest.mark.asyncio
async def test_spawn_from_persistent_definition_uses_stored_contract(tmp_path):
    captured: list = []
    store = SubagentDefinitionStore(tmp_path)
    store.save(
        SubagentSpec(
            name="scout",
            type="critic",
            allowed_tools=("read_file",),
            max_tool_rounds=3,
            system_prompt="Use the stored critic contract.",
        )
    )

    async def invoke(request):
        captured.append(request)
        return SimpleNamespace(content="ok", tokens_used=1)

    ctx = SubagentSpawnContext(parent_agent_id=uuid.uuid4(), parent_user_id=uuid.uuid4(), model=SimpleNamespace())
    handle = await spawn_subagent_from_definition(ctx, store, "scout", "review this", invoke=invoke)

    assert handle.result is not None and handle.result.ok
    assert captured[0].system_prompt_suffix == "Use the stored critic contract."
    assert captured[0].allowed_tool_names == ("read_file",)
    assert captured[0].max_tool_rounds == 3
