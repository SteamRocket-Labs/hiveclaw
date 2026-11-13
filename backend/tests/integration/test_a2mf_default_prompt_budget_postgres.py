"""A normal personal assistant with resident memory works without a model window."""

import json
from types import SimpleNamespace

import pytest

from tests.integration import test_r11c_colleague_lifecycle_postgres as lifecycle

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
def prompt_measurements(monkeypatch, request):
    from app.runtime import prompt_builder

    original = prompt_builder.assemble_runtime_prompt
    measurements = []

    def measure(frozen_prefix, dynamic_suffix, context_window_tokens=None, budget_profile=None):
        row = {
            "test": request.node.nodeid,
            "frozen_chars": len(frozen_prefix),
            "dynamic_chars": len(dynamic_suffix),
            "total_chars": len(prompt_builder._join_prompt_sections(frozen_prefix, dynamic_suffix)),
            "window": context_window_tokens,
            "budget": (budget_profile.system_prompt_budget_chars if budget_profile else
                       prompt_builder._compute_system_prompt_budget(context_window_tokens)),
        }
        measurements.append(row)
        try:
            result = original(frozen_prefix, dynamic_suffix, context_window_tokens, budget_profile)
        except prompt_builder.PromptBudgetExceededError:
            row["error"] = "PromptBudgetExceededError"
            raise
        finally:
            print("A2MF_PROMPT_MEASURE", json.dumps(row), flush=True)
        return result

    monkeypatch.setattr(prompt_builder, "assemble_runtime_prompt", measure)
    return measurements


async def _write_resident_memory(seed, owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    from datetime import UTC, datetime

    from app.memory.t3_platform_gate import apply_t3_consolidation_patch, file_sha256
    from app.memory.write_gate import MemoryWriteDecision
    from app.services import agent_tools
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call, new_owner_work
    from tests.memory.test_t3_consolidation_platform_gate import _accepted_review_for, _entry_patch

    async def scripted_review(content, *, category, **_kwargs):
        return MemoryWriteDecision(original_content=content, content=content, category=category,
            sensitivity="PL1_public", metadata={"threat_gate_method": "scripted_local_review"})

    monkeypatch.setattr("app.memory.explicit_overlay.prepare_memory_write_with_llm", scripted_review)
    n = SimpleNamespace(agent=seed["agent_id"], tenant=seed["tenant_id"], user=seed["user_id"],
        session=seed["session_id"], run=seed["run_id"], root=seed["agent_root"],
        service=agent_tools._get_tool_runtime_service())
    # Saved memories come from earlier work, not model rounds of this new run.
    n.session, n.run = await new_owner_work(n, owner_sessionmaker)
    saved, _ = await memory_call(n, app_user_sessionmaker, "save_memory",
        {"category": "feedback", "content": "Summarize verified facts in three concise sentences."})
    assert "Saved memory" in saved, saved
    manifest = n.root / "memory/explicit/manifest.jsonl"
    entry_id = json.loads(manifest.read_text().splitlines()[-1])["id"]
    ref = f"explicit://memory/{entry_id}"
    target = "memory/self/self.md"
    result = await apply_t3_consolidation_patch(agent_id=n.agent, tenant_id=n.tenant,
        data_root=seed["data_root"], job_id="a2mf-self-profile",
        revised_patch_md=_entry_patch(target, file_sha256(n.root / target), "concise-summaries",
            "### Concise summaries\n<!-- id: concise-summaries -->\nI summarize verified facts in three concise sentences.",
            refs=[ref]), review_md=_accepted_review_for(ref))
    assert result.status == "committed", result.issues
    saved, _ = await memory_call(n, app_user_sessionmaker, "save_memory",
        {"category": "project", "content": "The current release codename is ORCHID."})
    assert "Saved memory" in saved, saved
    from app.memory.explicit_overlay import load_explicit_overlay_entries

    assert sum(entry.status == "active" for entry in load_explicit_overlay_entries(seed["data_root"], n.agent)) == 1
    from app.models.runtime_task import RuntimeTask

    async with owner_sessionmaker() as db:
        prior = await db.get(RuntimeTask, n.run)
        prior.status, prior.completed_at = "completed", datetime.now(UTC)
        await db.commit()


@pytest.mark.parametrize("profile", ["skeleton", "resident"])
async def test_personal_assistant_without_declared_window_reaches_the_model(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, prompt_measurements, profile,
):
    import app.runtime.invoker as invoker
    from app.memory.profile_plane import EMPTY_SELF_CONTENT
    from app.models.agent import Agent
    from app.models.capability_policy import CapabilityPolicy
    from app.models.llm import LLMModel
    from app.models.runtime_task import RuntimeTask
    from app.models.user import User
    from app.services.assistant_binding import select_assistant

    seed, _, _ = await lifecycle._setup(owner_sessionmaker, app_user_sessionmaker, monkeypatch,
        tmp_path, tool="delegate_to_agent")
    async with owner_sessionmaker() as db:
        user = await db.get(User, seed["user_id"])
        user.role = "member"
        agent = await db.get(Agent, seed["agent_id"])
        model = await db.get(LLMModel, agent.primary_model_id)
        model.max_input_tokens = None
        binding = await select_assistant(db, user, agent.id)
        assert binding["applied"] is True, binding
        for capability in ("agent.memory.read", "agent.memory.write"):
            db.add(CapabilityPolicy(tenant_id=seed["tenant_id"], capability=capability,
                allowed=True, requires_approval=False))
        await db.commit()
    assert (seed["agent_root"] / "memory/self/self.md").read_text() == EMPTY_SELF_CONTENT
    if profile == "resident":
        await _write_resident_memory(seed, owner_sessionmaker, app_user_sessionmaker, monkeypatch)
    client = lifecycle.ScriptedLLMClient([{"content": "The requested summary is ready."}])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    await lifecycle._execute(seed["run_id"], owner_sessionmaker, worker="a2mf-personal-assistant")
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, seed["run_id"])
        print("A2MF_WEB_RESULT", json.dumps({"profile": profile, "status": run.status,
            "model_calls": len(client.requests)}), flush=True)
        assert run.status == "completed", {"status": run.status, "model_calls": len(client.requests)}
    assert len(client.requests) == 1
    assert prompt_measurements and all(row["window"] is None for row in prompt_measurements)
    if profile == "resident":
        wire = json.dumps(client.requests[0]["messages"], ensure_ascii=False)
        assert "I summarize verified facts in three concise sentences." in wire
        assert "ORCHID" in wire
