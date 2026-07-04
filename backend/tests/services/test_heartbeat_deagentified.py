from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = PROJECT_ROOT / "backend" / "app"


def test_heartbeat_service_no_longer_invokes_full_agent_runtime() -> None:
    source = (APP_ROOT / "services" / "heartbeat.py").read_text(encoding="utf-8")

    assert "invoke_agent" not in source
    assert "AgentInvocationRequest" not in source
    assert "ExecutionIdentityRef" not in source
    assert "execute_tool" not in source
    assert "_build_heartbeat_tool_executor" not in source
    assert "_HEARTBEAT_MAX_TOOL_ROUNDS" not in source


def test_t3_consolidator_template_is_direct_llm_core_contract() -> None:
    template = (APP_ROOT / "templates" / "T3_CONSOLIDATOR.md").read_text(encoding="utf-8")

    assert "direct LLM core" in template
    assert "submit_t3_" not in template
    assert '"consolidation_pitch_md"' in template
    assert '"revised_patch_md"' in template


@pytest.mark.asyncio
async def test_heartbeat_t3_core_writes_artifacts_without_tools(tmp_path, monkeypatch) -> None:
    from app.memory.t3_consolidation import build_t3_consolidation_batch
    from app.services import heartbeat_t3_core

    agent_id = uuid4()
    tenant_id = uuid4()
    package_dir = tmp_path / str(agent_id) / "memory" / "sessions" / "s1" / "segments" / "seg-1"
    package_dir.mkdir(parents=True)
    (package_dir / "summary.md").write_text(
        "<t2_summary><segment_state>complete</segment_state><content>owner likes concise updates</content></t2_summary>",
        encoding="utf-8",
    )
    (package_dir / "labels.md").write_text(
        "<t2_labels><continuity_state>standalone</continuity_state><topic>preference</topic></t2_labels>",
        encoding="utf-8",
    )
    (package_dir / "review.md").write_text(
        "<t2_review><decision>approved</decision><allowed_next>t3_intake</allowed_next></t2_review>",
        encoding="utf-8",
    )
    (package_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "t2.segment-package.manifest.v1",
                "package_status": "reviewed",
                "source_refs": ["t0://session/s1/segment/seg-1#seq=1..2"],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    staged = build_t3_consolidation_batch(agent_id=agent_id, data_root=tmp_path, package_dirs=[package_dir])

    calls: list[dict] = []

    class FakeClient:
        async def complete(self, *, messages, tools=None, temperature=0.0, max_tokens=None, **_kwargs):
            calls.append({"messages": messages, "tools": tools, "temperature": temperature, "max_tokens": max_tokens})
            if len(calls) == 1:
                payload = {
                    "summary": "curated owner update preference",
                    "consolidation_pitch_md": "# T3 Consolidation Pitch\n\n- t2://session/s1/segment/seg-1",
                    "revised_patch_md": (
                        "# T3 Revised Patch\n\n"
                        '<t3_consolidation_patch schema_version="t3.consolidation_patch.v1"></t3_consolidation_patch>'
                    ),
                }
            else:
                payload = {
                    "review_md": (
                        "# T3 Memory Gate Review\n\n"
                        '<memory_gate_review schema_version="t3.memory_gate_review.v1">'
                        "<decision>accept</decision>"
                        "</memory_gate_review>"
                    )
                }
            return SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))

        async def close(self):
            return None

    gate_calls: list[dict] = []

    def fake_apply_gate(**kwargs):
        gate_calls.append(kwargs)
        return SimpleNamespace(
            status="committed",
            issues=(),
            committed_paths=("memory/profiles/owner.md",),
            committed_blocks=("pref-concise-updates",),
        )

    monkeypatch.setattr(heartbeat_t3_core, "create_llm_client_from_config", lambda _config: FakeClient())
    monkeypatch.setattr(heartbeat_t3_core, "apply_t3_consolidation_patch", fake_apply_gate)

    result = await heartbeat_t3_core.run_heartbeat_t3_core(
        agent_id=agent_id,
        tenant_id=tenant_id,
        data_root=tmp_path,
        model=SimpleNamespace(
            provider="openai",
            model="gpt-4.1",
            api_key="key",
            base_url=None,
            max_output_tokens=8192,
        ),
    )

    assert result.status == "committed"
    assert result.job_id == staged.job_id
    assert len(calls) == 2
    assert calls[0]["tools"] is None
    assert calls[1]["tools"] is None
    assert gate_calls

    job_dir = tmp_path / str(agent_id) / "memory" / ".staging" / "t3_jobs" / staged.job_id
    assert "curated owner update preference" in (job_dir / "manifest.json").read_text(encoding="utf-8")
    assert "T3 Consolidation Pitch" in (job_dir / "consolidation_pitch.md").read_text(encoding="utf-8")
    assert "t3_consolidation_patch" in (job_dir / "revised_patch.md").read_text(encoding="utf-8")
    assert "memory_gate_review" in (job_dir / "review.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_heartbeat_t3_core_skips_llm_when_no_pending_sources(tmp_path, monkeypatch) -> None:
    from app.services import heartbeat_t3_core

    def fail_client(_config):  # pragma: no cover - must not be called
        raise AssertionError("heartbeat direct core must not call LLM when there are no pending T3 sources")

    monkeypatch.setattr(heartbeat_t3_core, "create_llm_client_from_config", fail_client)

    result = await heartbeat_t3_core.run_heartbeat_t3_core(
        agent_id=uuid4(),
        tenant_id=uuid4(),
        data_root=tmp_path,
        model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None),
    )

    assert result.status == "skipped"
    assert result.skip_reason == "no_pending_t3_sources"
