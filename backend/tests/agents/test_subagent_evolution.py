"""Evolution loop P1 (docs/subagent-evolution-loop.md): nomination + drafting.

Threshold-gated, agent-level-only, fail-soft. The proposal is a file —
never applied here; P2's approval surface is the only definition writer.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

import app.agents.subagent_evolution as evo_mod
from app.agents import subagent_memory as mem_mod
from app.agents.subagent import SubagentSpec
from app.agents.subagent_definition import definition_store_for_agent
from app.agents.subagent_evolution import (
    EVOLUTION_DRAFT_SYSTEM_PROMPT,
    EvolutionProposal,
    SubagentProposalStore,
    maybe_nominate,
    proposal_store_for_agent,
)
from app.agents.subagent_memory import SubagentMemoryStore

_MODEL_CONFIG = {"provider": "openai", "api_key": "k", "model": "m", "base_url": None}


def _proposal(**overrides) -> EvolutionProposal:
    fields = dict(
        name="scout",
        status="pending",
        base_definition_sha="abc123",
        absorbed_entry_ids=["e1", "e2"],
        rationale="two lessons proved general",
        created_at="2026-06-05T00:00:00+00:00",
        proposal_id="prop-1",
        body="You are a scout. Always verify sources twice.",
    )
    fields.update(overrides)
    return EvolutionProposal(**fields)


def _seed_memory(store: SubagentMemoryStore, monkeypatch, n: int) -> list[str]:
    ids: list[str] = []

    def decision(content, **kwargs):
        entry_id = f"e{len(ids) + 1}"
        ids.append(entry_id)
        return SimpleNamespace(
            rejected=False, reason="", content=content, metadata={"entry_id": entry_id, "sensitivity": "internal"}
        )

    monkeypatch.setattr(mem_mod, "prepare_memory_write", decision)
    for i in range(n):
        store.record_how("scout", f"lesson {i + 1}", category="pitfall")
    return ids


def _draft_response(absorbed: list[str]) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "body": "You are a scout. Always verify sources twice.",
                            "absorbed_entry_ids": absorbed,
                            "rationale": "general lessons",
                        }
                    )
                }
            }
        ]
    }


# --- prompt + store ------------------------------------------------------------


def test_draft_prompt_is_vendor_neutral():
    lowered = EVOLUTION_DRAFT_SYSTEM_PROMPT.lower()
    for vendor in ("claude", "anthropic", "gpt", "openai", "gemini", "deepseek"):
        assert vendor not in lowered
    assert "absorbed_entry_ids" in EVOLUTION_DRAFT_SYSTEM_PROMPT


def test_proposal_store_roundtrip(tmp_path):
    store = SubagentProposalStore(tmp_path)
    store.save(_proposal())

    loaded = store.load("scout")
    assert loaded is not None
    assert loaded.status == "pending"
    assert loaded.base_definition_sha == "abc123"
    assert loaded.absorbed_entry_ids == ["e1", "e2"]
    assert loaded.body == "You are a scout. Always verify sources twice."
    assert store.load_pending("scout") is not None

    store.set_status("scout", "approved")
    assert store.load_pending("scout") is None
    reread = store.load("scout")
    assert reread is not None and reread.status == "approved"


def test_proposal_store_rejects_path_traversal(tmp_path):
    store = SubagentProposalStore(tmp_path)
    with pytest.raises(ValueError, match="subagent name"):
        store.save(_proposal(name="../escape"))


# --- nomination ----------------------------------------------------------------


def _agent_definition(agent_id, tmp_path, body: str = "You are a scout.") -> None:
    definition_store_for_agent(agent_id, agent_data_dir=tmp_path).save(
        SubagentSpec(name="scout", description="d", type="explorer", system_prompt=body)
    )


@pytest.mark.asyncio
async def test_nominate_creates_pending_proposal(tmp_path, monkeypatch):
    agent_id = uuid.uuid4()
    _agent_definition(agent_id, tmp_path)
    memory_store = SubagentMemoryStore(tmp_path / "mem")
    ids = _seed_memory(memory_store, monkeypatch, n=8)

    async def fake_chat_complete(**kwargs):
        return _draft_response(absorbed=[ids[0], ids[1], "ghost-id"])

    notified: list = []

    async def fake_notify(agent_id, spec_name, proposal_id, **kwargs):
        notified.append((spec_name, proposal_id))

    monkeypatch.setattr(evo_mod, "chat_complete", fake_chat_complete)
    monkeypatch.setattr(evo_mod, "_notify_owner", fake_notify)

    proposal = await maybe_nominate(
        agent_id=agent_id,
        spec_name="scout",
        memory_store=memory_store,
        model_config=_MODEL_CONFIG,
        threshold=8,
        agent_data_dir=tmp_path,
    )

    assert proposal is not None
    assert proposal.status == "pending"
    # ghost ids are filtered — only entries that exist in active memory count
    assert proposal.absorbed_entry_ids == [ids[0], ids[1]]
    assert proposal.base_definition_sha
    assert notified == [("scout", proposal.proposal_id)]
    # persisted
    assert proposal_store_for_agent(agent_id, agent_data_dir=tmp_path).load_pending("scout") is not None


@pytest.mark.asyncio
async def test_nominate_skips_oversized_body_without_llm_call(tmp_path, monkeypatch):
    """Limit-coherence guard: a body too large to round-trip the 8192 draft
    budget must skip nomination loudly instead of failing every tick."""
    agent_id = uuid.uuid4()
    _agent_definition(agent_id, tmp_path, body="x" * 25_000)
    memory_store = SubagentMemoryStore(tmp_path / "mem")
    _seed_memory(memory_store, monkeypatch, n=9)

    async def fail_chat(**kwargs):
        raise AssertionError("LLM must not be called for an oversized body")

    monkeypatch.setattr(evo_mod, "chat_complete", fail_chat)

    assert (
        await maybe_nominate(
            agent_id=agent_id,
            spec_name="scout",
            memory_store=memory_store,
            model_config=_MODEL_CONFIG,
            threshold=8,
            agent_data_dir=tmp_path,
        )
        is None
    )


@pytest.mark.asyncio
async def test_nominate_skips_below_threshold_without_llm_call(tmp_path, monkeypatch):
    agent_id = uuid.uuid4()
    _agent_definition(agent_id, tmp_path)
    memory_store = SubagentMemoryStore(tmp_path / "mem")
    _seed_memory(memory_store, monkeypatch, n=3)

    async def fail_chat(**kwargs):
        raise AssertionError("LLM must not be called below threshold")

    monkeypatch.setattr(evo_mod, "chat_complete", fail_chat)

    assert (
        await maybe_nominate(
            agent_id=agent_id,
            spec_name="scout",
            memory_store=memory_store,
            model_config=_MODEL_CONFIG,
            threshold=8,
            agent_data_dir=tmp_path,
        )
        is None
    )


@pytest.mark.asyncio
async def test_nominate_skips_without_agent_level_definition(tmp_path, monkeypatch):
    # tenant/builtin/inline specs have no agent-level definition file → no nomination
    agent_id = uuid.uuid4()
    memory_store = SubagentMemoryStore(tmp_path / "mem")
    _seed_memory(memory_store, monkeypatch, n=10)

    assert (
        await maybe_nominate(
            agent_id=agent_id,
            spec_name="scout",
            memory_store=memory_store,
            model_config=_MODEL_CONFIG,
            threshold=8,
            agent_data_dir=tmp_path,
        )
        is None
    )


@pytest.mark.asyncio
async def test_nominate_mutex_on_pending_proposal(tmp_path, monkeypatch):
    agent_id = uuid.uuid4()
    _agent_definition(agent_id, tmp_path)
    memory_store = SubagentMemoryStore(tmp_path / "mem")
    _seed_memory(memory_store, monkeypatch, n=9)
    proposal_store_for_agent(agent_id, agent_data_dir=tmp_path).save(_proposal())

    async def fail_chat(**kwargs):
        raise AssertionError("LLM must not be called while a proposal is pending")

    monkeypatch.setattr(evo_mod, "chat_complete", fail_chat)

    assert (
        await maybe_nominate(
            agent_id=agent_id,
            spec_name="scout",
            memory_store=memory_store,
            model_config=_MODEL_CONFIG,
            threshold=8,
            agent_data_dir=tmp_path,
        )
        is None
    )


@pytest.mark.asyncio
async def test_nominate_fails_soft_on_garbage_draft(tmp_path, monkeypatch):
    agent_id = uuid.uuid4()
    _agent_definition(agent_id, tmp_path)
    memory_store = SubagentMemoryStore(tmp_path / "mem")
    _seed_memory(memory_store, monkeypatch, n=8)

    async def fake_chat_complete(**kwargs):
        return {"choices": [{"message": {"content": "not json at all"}}]}

    monkeypatch.setattr(evo_mod, "chat_complete", fake_chat_complete)

    assert (
        await maybe_nominate(
            agent_id=agent_id,
            spec_name="scout",
            memory_store=memory_store,
            model_config=_MODEL_CONFIG,
            threshold=8,
            agent_data_dir=tmp_path,
        )
        is None
    )
    assert proposal_store_for_agent(agent_id, agent_data_dir=tmp_path).load("scout") is None


# --- P2: apply / reject (the only definition writers) ---------------------------


def _nominated(tmp_path, monkeypatch, agent_id, *, n_entries: int = 8) -> tuple[list[str], EvolutionProposal]:
    """Seed definition + memory + a real pending proposal whose base_sha matches."""

    from app.agents.subagent_evolution import definition_sha
    from app.agents.subagent_definition import render_subagent_definition

    _agent_definition(agent_id, tmp_path)
    memory_store = SubagentMemoryStore(tmp_path / str(agent_id) / "subagents" / ".memory")
    ids = _seed_memory(memory_store, monkeypatch, n=n_entries)
    base_spec = definition_store_for_agent(agent_id, agent_data_dir=tmp_path).load("scout")
    proposal = _proposal(
        base_definition_sha=definition_sha(render_subagent_definition(base_spec)),
        absorbed_entry_ids=ids[:2],
    )
    proposal_store_for_agent(agent_id, agent_data_dir=tmp_path).save(proposal)
    return ids, proposal


def test_apply_proposal_writes_definition_marks_absorbed_and_ledgers(tmp_path, monkeypatch):
    from app.agents.subagent_evolution import apply_proposal

    agent_id = uuid.uuid4()
    ids, proposal = _nominated(tmp_path, monkeypatch, agent_id)

    result = apply_proposal(agent_id, "scout", approved_by="rocky", agent_data_dir=tmp_path)

    assert result.applied is True
    # definition body replaced; frontmatter (contract) untouched by construction
    updated = definition_store_for_agent(agent_id, agent_data_dir=tmp_path).load("scout")
    assert updated.system_prompt == proposal.body
    assert updated.type == "explorer" and updated.description == "d"
    # absorbed entries retired
    memory_store = SubagentMemoryStore(tmp_path / str(agent_id) / "subagents" / ".memory")
    active = memory_store.load("scout", active_only=True)
    assert "lesson 1" not in active and "lesson 2" not in active
    assert "lesson 3" in active
    # proposal closed
    assert proposal_store_for_agent(agent_id, agent_data_dir=tmp_path).load_pending("scout") is None
    # ledger row written
    ledger = (tmp_path / str(agent_id) / "evolution" / "evolution_ledger.jsonl").read_text()
    assert "subagent_definition_promotion" in ledger
    assert '"approved"' in ledger and "rocky" in ledger


def test_apply_proposal_stale_base_is_rejected(tmp_path, monkeypatch):
    from app.agents.subagent_evolution import apply_proposal

    agent_id = uuid.uuid4()
    _nominated(tmp_path, monkeypatch, agent_id)
    # someone edits the definition AFTER the proposal was drafted
    definition_store_for_agent(agent_id, agent_data_dir=tmp_path).save(
        SubagentSpec(name="scout", description="d", type="explorer", system_prompt="manually edited body")
    )

    result = apply_proposal(agent_id, "scout", approved_by="rocky", agent_data_dir=tmp_path)

    assert result.applied is False
    assert result.error == "stale_base"
    # stale proposal is auto-closed (rejected) so nomination can run again
    assert proposal_store_for_agent(agent_id, agent_data_dir=tmp_path).load_pending("scout") is None
    updated = definition_store_for_agent(agent_id, agent_data_dir=tmp_path).load("scout")
    assert updated.system_prompt == "manually edited body"  # untouched


def test_apply_proposal_without_pending_returns_error(tmp_path):
    from app.agents.subagent_evolution import apply_proposal

    result = apply_proposal(uuid.uuid4(), "scout", approved_by="rocky", agent_data_dir=tmp_path)
    assert result.applied is False
    assert result.error == "no_pending"


def test_reject_proposal_closes_and_ledgers(tmp_path, monkeypatch):
    from app.agents.subagent_evolution import reject_proposal

    agent_id = uuid.uuid4()
    _nominated(tmp_path, monkeypatch, agent_id)

    assert reject_proposal(agent_id, "scout", rejected_by="rocky", agent_data_dir=tmp_path) is True
    assert proposal_store_for_agent(agent_id, agent_data_dir=tmp_path).load_pending("scout") is None
    # memory untouched on reject
    memory_store = SubagentMemoryStore(tmp_path / str(agent_id) / "subagents" / ".memory")
    assert memory_store.count_active_entries("scout") == 8
    ledger = (tmp_path / str(agent_id) / "evolution" / "evolution_ledger.jsonl").read_text()
    assert '"rejected"' in ledger


@pytest.mark.asyncio
async def test_auto_mode_applies_immediately(tmp_path, monkeypatch):
    """auto mode: nomination → immediate apply via the SAME path, approved_by=auto."""

    agent_id = uuid.uuid4()
    _agent_definition(agent_id, tmp_path)
    memory_store = SubagentMemoryStore(tmp_path / str(agent_id) / "subagents" / ".memory")
    ids = _seed_memory(memory_store, monkeypatch, n=8)

    async def fake_chat_complete(**kwargs):
        return _draft_response(absorbed=ids[:2])

    async def auto_on(agent_id):
        return True

    notified: list = []

    async def fake_notify(*args, **kwargs):
        notified.append(args)

    monkeypatch.setattr(evo_mod, "chat_complete", fake_chat_complete)
    monkeypatch.setattr(evo_mod, "_auto_approve_enabled", auto_on)
    monkeypatch.setattr(evo_mod, "_notify_owner", fake_notify)

    proposal = await maybe_nominate(
        agent_id=agent_id,
        spec_name="scout",
        memory_store=memory_store,
        model_config=_MODEL_CONFIG,
        threshold=8,
        agent_data_dir=tmp_path,
    )

    assert proposal is not None
    # applied immediately: definition updated, proposal closed, approved_by=auto in ledger
    updated = definition_store_for_agent(agent_id, agent_data_dir=tmp_path).load("scout")
    assert "verify sources twice" in updated.system_prompt
    assert proposal_store_for_agent(agent_id, agent_data_dir=tmp_path).load_pending("scout") is None
    ledger = (tmp_path / str(agent_id) / "evolution" / "evolution_ledger.jsonl").read_text()
    assert '"auto"' in ledger


@pytest.mark.asyncio
async def test_manual_mode_leaves_proposal_pending(tmp_path, monkeypatch):
    agent_id = uuid.uuid4()
    _agent_definition(agent_id, tmp_path)
    memory_store = SubagentMemoryStore(tmp_path / str(agent_id) / "subagents" / ".memory")
    ids = _seed_memory(memory_store, monkeypatch, n=8)

    async def fake_chat_complete(**kwargs):
        return _draft_response(absorbed=ids[:2])

    async def auto_off(agent_id):
        return False

    monkeypatch.setattr(evo_mod, "chat_complete", fake_chat_complete)
    monkeypatch.setattr(evo_mod, "_auto_approve_enabled", auto_off)
    monkeypatch.setattr(evo_mod, "_notify_owner", lambda *a, **k: _async_none())

    proposal = await maybe_nominate(
        agent_id=agent_id,
        spec_name="scout",
        memory_store=memory_store,
        model_config=_MODEL_CONFIG,
        threshold=8,
        agent_data_dir=tmp_path,
    )

    assert proposal is not None
    assert proposal_store_for_agent(agent_id, agent_data_dir=tmp_path).load_pending("scout") is not None
    # definition untouched until a human approves
    assert (
        definition_store_for_agent(agent_id, agent_data_dir=tmp_path).load("scout").system_prompt == "You are a scout."
    )


async def _async_none():
    return None


@pytest.mark.asyncio
async def test_spawn_distillation_triggers_nomination(tmp_path, monkeypatch):
    """Wiring: a successful distillation write checks for nomination (fire-safe)."""

    from app.agents.subagent import SubagentJob, SubagentSpawnContext, _record_memory_from_result, SubagentResult

    calls: list = []

    async def fake_nominate(**kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(evo_mod, "maybe_nominate", fake_nominate)
    monkeypatch.setattr(
        mem_mod,
        "prepare_memory_write",
        lambda content, **kw: SimpleNamespace(
            rejected=False, reason="", content=content, metadata={"entry_id": "e1", "sensitivity": "internal"}
        ),
    )

    memory_store = SubagentMemoryStore(tmp_path / "mem")
    ctx = SubagentSpawnContext(
        parent_agent_id=uuid.uuid4(),
        parent_user_id=uuid.uuid4(),
        model=SimpleNamespace(provider="openai", api_key="k", model="m", base_url=None),
        memory_store=memory_store,
        memory_distiller=lambda run_log: [("pitfall", "Watch for truncated tables.")],
    )
    spec = SubagentSpec(name="scout", type="explorer")
    result = SubagentResult(name="scout", type="explorer", status="completed", content="ok")

    await _record_memory_from_result(ctx, SubagentJob(spec=spec, task="t"), result)

    assert len(calls) == 1
    assert calls[0]["spec_name"] == "scout"
    assert calls[0]["memory_store"] is memory_store
