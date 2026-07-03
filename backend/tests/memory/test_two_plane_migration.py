"""Part H red tests: C7 migration — legacy four files → two planes (spec §6.3).

The migration is the only irreversible step in the mainline, so it is a
dry-run-first tool (safety gate, not an MVP stage): content reorganization is
an LLM judgment over the FULL legacy corpus (soul purity split, worker
constraint two-way split, capabilities three-way split, episodes→milestones
selection); apply lands the new planes atomically, archives the legacy files
to memory/.archive/legacy_t3/ (never deletes), and records a migration marker
for idempotency. No model config → held, data untouched.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


def _mem_dir(tmp_path: Path, agent_id) -> Path:
    return tmp_path / str(agent_id) / "memory"


def _seed_legacy_agent(tmp_path: Path, agent_id) -> None:
    root = tmp_path / str(agent_id)
    root.mkdir(parents=True, exist_ok=True)
    (root / "soul.md").write_text(
        "# Soul\n## Identity\n研究助理。\n## How I Learn\n我实际擅长深度研究,常在需求含糊时猜测。\n",
        encoding="utf-8",
    )
    t3 = root / "memory" / "t3"
    t3.mkdir(parents=True, exist_ok=True)
    (t3 / "user.md").write_text(
        '<t3_user_memory id="um-1" status="active"><claim>偏好中文汇报。</claim>'
        "<evidence><source_ref>t2://session/s1/segment/seg-1</source_ref></evidence></t3_user_memory>\n",
        encoding="utf-8",
    )
    (t3 / "worker.md").write_text(
        '<t3_worker_rule id="wr-1" status="active"><rule>不得外发未审内容。</rule></t3_worker_rule>\n'
        '<t3_worker_rule id="wr-2" status="active"><rule>长任务我容易忘记推进,需要 ledger。</rule></t3_worker_rule>\n',
        encoding="utf-8",
    )
    (t3 / "capabilities.md").write_text(
        '<t3_capability id="cap-1" status="active"><name>L2 扩容知识</name>'
        "<claim>L2 通过链下计算扩容。</claim></t3_capability>\n",
        encoding="utf-8",
    )
    (t3 / "episodes.md").write_text(
        '<t3_episode id="ep-1" status="active"><what_happened>首次交付完整研报,owner 好评。</what_happened></t3_episode>\n',
        encoding="utf-8",
    )


FAKE_PLAN = {
    "soul_md": "# Soul\n## Identity\n研究助理。\n",
    "self_md": (
        "## 能力\n\n### 深度研究 — 熟练\n<!-- id: cap-deep-research -->\n"
        "拆解、多源检索。\n- 证据: t2-a1b2\n\n## 失败模式\n\n"
        "### 需求含糊时爱猜 — active\n<!-- id: fm-guessing -->\n- 状态: active\n- 证据: t2-a1b2\n"
    ),
    "profiles": {
        "owner": "## 偏好\n\n### 中文汇报 — 已确认\n<!-- id: pref-lang -->\n偏好中文汇报。\n- 证据: t2-a1b2\n",
        "collaborators": "",
        "domain": "",
    },
    "knowledge_pages": [
        {
            "slug": "l2-rollup",
            "content": (
                "---\ntitle: L2 Rollup\nstatus: active\n---\n## Current Claim\nL2 通过链下计算扩容。\n"
                "## Evidence\nt2-a1b2\n\n## Relations\n- is_a [[k:Scaling]]\n"
            ),
        }
    ],
    "milestone_pages": [
        {
            "slug": "ms-first-report",
            "content": "---\ntitle: 首份研报\nstatus: active\n---\n首次交付,owner 好评。\n证据: t2-a1b2\n",
        }
    ],
    "notes": "worker wr-1 硬约束→soul 候选;wr-2 软边界→self 失败模式。",
}


async def _fake_llm_plan(*_args, **_kwargs) -> dict:
    return FAKE_PLAN


@pytest.mark.asyncio
async def test_dry_run_produces_plan_without_touching_data(tmp_path: Path) -> None:
    from app.scripts.migrate_memory_two_planes import migrate_agent_memory

    agent_id = uuid4()
    _seed_legacy_agent(tmp_path, agent_id)

    report = await migrate_agent_memory(agent_id=agent_id, data_root=tmp_path, apply=False, plan_builder=_fake_llm_plan)

    assert report.status == "planned"
    assert report.plan_path is not None and report.plan_path.exists()
    # data untouched
    assert (_mem_dir(tmp_path, agent_id) / "t3" / "user.md").exists()
    assert not (_mem_dir(tmp_path, agent_id) / "self" / "self.md").exists()
    plan = json.loads(report.plan_path.read_text(encoding="utf-8"))
    assert plan["self_md"]


@pytest.mark.asyncio
async def test_apply_lands_planes_archives_legacy_and_marks(tmp_path: Path) -> None:
    from app.scripts.migrate_memory_two_planes import migrate_agent_memory

    agent_id = uuid4()
    _seed_legacy_agent(tmp_path, agent_id)

    report = await migrate_agent_memory(agent_id=agent_id, data_root=tmp_path, apply=True, plan_builder=_fake_llm_plan)

    assert report.status == "applied"
    mem = _mem_dir(tmp_path, agent_id)
    assert "深度研究" in (mem / "self" / "self.md").read_text(encoding="utf-8")
    assert "中文汇报" in (mem / "profiles" / "owner.md").read_text(encoding="utf-8")
    assert (mem / "knowledge" / "l2-rollup.md").exists()
    assert (mem / "milestones" / "ms-first-report.md").exists()
    # soul replaced with the purified version
    assert "How I Learn" not in (tmp_path / str(agent_id) / "soul.md").read_text(encoding="utf-8")
    # legacy files archived — moved, never deleted
    assert not (mem / "t3" / "user.md").exists()
    archive_root = mem / ".archive" / "legacy_t3"
    archived = list(archive_root.glob("*/user.md"))
    assert archived and "偏好中文汇报" in archived[0].read_text(encoding="utf-8")
    assert list(archive_root.glob("*/soul.md"))  # old soul archived too
    # marker for idempotency
    marker = json.loads((mem / "control" / "two_plane_migration.json").read_text(encoding="utf-8"))
    assert marker["status"] == "applied"


@pytest.mark.asyncio
async def test_migration_is_idempotent(tmp_path: Path) -> None:
    from app.scripts.migrate_memory_two_planes import migrate_agent_memory

    agent_id = uuid4()
    _seed_legacy_agent(tmp_path, agent_id)
    first = await migrate_agent_memory(agent_id=agent_id, data_root=tmp_path, apply=True, plan_builder=_fake_llm_plan)
    assert first.status == "applied"

    calls = {"count": 0}

    async def counting_plan(*args, **kwargs):
        calls["count"] += 1
        return FAKE_PLAN

    second = await migrate_agent_memory(agent_id=agent_id, data_root=tmp_path, apply=True, plan_builder=counting_plan)

    assert second.status == "already_migrated"
    assert calls["count"] == 0


@pytest.mark.asyncio
async def test_no_plan_builder_holds_without_touching_data(tmp_path: Path) -> None:
    from app.scripts.migrate_memory_two_planes import migrate_agent_memory

    agent_id = uuid4()
    _seed_legacy_agent(tmp_path, agent_id)

    report = await migrate_agent_memory(agent_id=agent_id, data_root=tmp_path, apply=True, plan_builder=None)

    assert report.status == "held"
    assert (_mem_dir(tmp_path, agent_id) / "t3" / "user.md").exists()
    assert not (_mem_dir(tmp_path, agent_id) / "self" / "self.md").exists()


@pytest.mark.asyncio
async def test_invalid_plan_holds(tmp_path: Path) -> None:
    from app.scripts.migrate_memory_two_planes import migrate_agent_memory

    agent_id = uuid4()
    _seed_legacy_agent(tmp_path, agent_id)

    async def broken_plan(*_a, **_k) -> dict:
        return {"self_md": ""}  # missing required fields

    report = await migrate_agent_memory(agent_id=agent_id, data_root=tmp_path, apply=True, plan_builder=broken_plan)

    assert report.status == "held"
    assert report.issues
    assert (_mem_dir(tmp_path, agent_id) / "t3" / "user.md").exists()


@pytest.mark.asyncio
async def test_empty_self_plan_applies_with_no_claim_scaffold(tmp_path: Path) -> None:
    from app.scripts.migrate_memory_two_planes import migrate_agent_memory

    agent_id = uuid4()
    _seed_legacy_agent(tmp_path, agent_id)

    async def empty_self_plan(*_a, **_k) -> dict:
        plan = dict(FAKE_PLAN)
        plan["self_md"] = ""
        return plan

    report = await migrate_agent_memory(agent_id=agent_id, data_root=tmp_path, apply=True, plan_builder=empty_self_plan)

    assert report.status == "applied"
    mem = _mem_dir(tmp_path, agent_id)
    assert "No accepted self observations migrated" in (mem / "self" / "self.md").read_text(encoding="utf-8")
    assert not (mem / "t3" / "user.md").exists()


@pytest.mark.asyncio
async def test_empty_soul_plan_holds_when_original_soul_has_content(tmp_path: Path) -> None:
    from app.scripts.migrate_memory_two_planes import migrate_agent_memory

    agent_id = uuid4()
    _seed_legacy_agent(tmp_path, agent_id)

    async def empty_soul_plan(*_a, **_k) -> dict:
        plan = dict(FAKE_PLAN)
        plan["soul_md"] = ""
        return plan

    report = await migrate_agent_memory(agent_id=agent_id, data_root=tmp_path, apply=True, plan_builder=empty_soul_plan)

    assert report.status == "held"
    assert "plan missing soul_md" in report.issues
    assert (_mem_dir(tmp_path, agent_id) / "t3" / "user.md").exists()


@pytest.mark.asyncio
async def test_empty_soul_plan_applies_with_no_claim_scaffold_when_original_soul_empty(tmp_path: Path) -> None:
    from app.scripts.migrate_memory_two_planes import migrate_agent_memory

    agent_id = uuid4()
    _seed_legacy_agent(tmp_path, agent_id)
    (tmp_path / str(agent_id) / "soul.md").write_text("", encoding="utf-8")

    async def empty_soul_plan(*_a, **_k) -> dict:
        plan = dict(FAKE_PLAN)
        plan["soul_md"] = ""
        return plan

    report = await migrate_agent_memory(agent_id=agent_id, data_root=tmp_path, apply=True, plan_builder=empty_soul_plan)

    assert report.status == "applied"
    soul = (tmp_path / str(agent_id) / "soul.md").read_text(encoding="utf-8")
    assert "No soul content available before migration" in soul
    assert not (_mem_dir(tmp_path, agent_id) / "t3" / "user.md").exists()


@pytest.mark.asyncio
async def test_agent_without_legacy_data_is_noop(tmp_path: Path) -> None:
    from app.scripts.migrate_memory_two_planes import migrate_agent_memory

    agent_id = uuid4()
    (tmp_path / str(agent_id) / "memory").mkdir(parents=True)

    report = await migrate_agent_memory(agent_id=agent_id, data_root=tmp_path, apply=True, plan_builder=_fake_llm_plan)

    assert report.status == "no_legacy_data"


def test_iter_agent_ids_ignores_control_directories(tmp_path: Path) -> None:
    from app.scripts.migrate_memory_two_planes import _iter_agent_ids

    first = uuid4()
    second = uuid4()
    (tmp_path / ".hive").mkdir()
    (tmp_path / ".failed_extractions").mkdir()
    (tmp_path / "not-an-agent").mkdir()
    (tmp_path / str(second)).mkdir()
    (tmp_path / str(first)).mkdir()

    assert list(_iter_agent_ids(tmp_path)) == sorted([str(first), str(second)])


def test_script_secrets_provider_initializes_for_standalone_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.scripts.migrate_memory_two_planes import _init_script_secrets_provider
    from app.services import secrets_provider

    monkeypatch.setattr(secrets_provider, "_provider", None)

    _init_script_secrets_provider(SimpleNamespace(SECRETS_MASTER_KEY="x" * 32, DEBUG=False))

    provider = secrets_provider.get_secrets_provider()
    encrypted = provider.encrypt("secret-value")
    assert encrypted != "secret-value"
    assert provider.decrypt(encrypted) == "secret-value"
