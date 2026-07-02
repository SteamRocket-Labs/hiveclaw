"""Knowledge read model tests (docs/agent-memory-md-first-spec.md §12 P7).

Acceptance:
- Frontend no longer parses raw file layout for primary view.
- Overview, entries, timeline, and candidates are structured.
- Raw Markdown remains available as advanced view (workspace file APIs —
  untouched here).
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.services.knowledge_read_model import (
    build_knowledge_overview,
    get_knowledge_page,
    list_knowledge_candidates,
    list_knowledge_entries,
    list_knowledge_events,
    list_knowledge_pages,
)

AGENT = uuid.uuid4()


def _seed_workspace(tmp_path: Path) -> Path:
    root = tmp_path / str(AGENT)
    mem = root / "memory"
    (mem / "self").mkdir(parents=True)
    (mem / "profiles").mkdir(parents=True)
    (mem / "knowledge").mkdir(parents=True)
    (mem / "milestones").mkdir(parents=True)
    (mem / "control").mkdir(parents=True)
    (root / "evolution").mkdir(parents=True)
    (root / "skills" / "market-research").mkdir(parents=True)

    (root / "soul.md").write_text(
        "---\n"
        "schema: hive.soul.v2\n"
        "---\n\n"
        '<soul_identity id="identity" status="active" frozen="true"><claim>agent</claim></soul_identity>\n'
        '<soul_user_model id="user-model" status="active"><claim>x</claim></soul_user_model>\n',
        encoding="utf-8",
    )
    (root / "skills" / "market-research" / "SKILL.md").write_text("---\nname: market-research\n---\n", encoding="utf-8")

    recent = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    del recent
    (mem / "self" / "self.md").write_text(
        "## 方法\n\n"
        "### 研究三段法 — 熟练\n<!-- id: mem_s1 -->\nresearch → design → verify workflow\n"
        "- skill候选: 三次验证有效\n- 证据: t2-a1b2\n\n"
        "### 夜间摘要管线 — 一般\n<!-- id: mem_s2 -->\nnightly digest pipeline with durable state\n"
        "- workflow候选: 需要持久状态\n- 证据: t2-c3d4\n",
        encoding="utf-8",
    )
    (mem / "profiles" / "owner.md").write_text(
        "## 偏好\n\n### 联系方式 — 已确认\n<!-- id: mem_f1 -->\nuser email is <Email_1>\n- 证据: t2-e5f6\n",
        encoding="utf-8",
    )

    (mem / "knowledge" / "memory-control-plane.md").write_text(
        "---\ntitle: Memory Control Plane\ntype: concept\ntags: [memory]\nstatus: active\n---\n\n"
        "## Current Claim\n\nx\n",
        encoding="utf-8",
    )
    (mem / "milestones" / "railway-deployments.md").write_text(
        "---\ntitle: Railway Deployments\ntype: scene\nstatus: active\n---\n\n## Narrative\n\nx\n",
        encoding="utf-8",
    )

    audit_rows = [
        {"at": "2026-06-04T10:00:00+00:00", "stage": "scene_curation", "outcome": "held", "reason": "no LLM"},
        {"at": "2026-06-04T11:00:00+00:00", "stage": "wiki_apply", "outcome": "applied", "reason": "ok"},
        {
            "at": "2026-06-04T12:00:00+00:00",
            "stage": "soul_candidate",
            "outcome": "held",
            "reason": "thin evidence",
            "detail": {
                "candidate_id": "cand1",
                "candidate_package_path": "memory/.staging/soul_candidates/cand1",
                "target_path": "soul.md",
            },
        },
    ]
    (mem / "distillation_audit.jsonl").write_text(
        "\n".join(json.dumps(row) for row in audit_rows) + "\n", encoding="utf-8"
    )
    soul_candidate = mem / ".staging" / "soul_candidates" / "cand1" / "manifest.json"
    soul_candidate.parent.mkdir(parents=True)
    soul_candidate.write_text(
        json.dumps(
            {
                "schema": "soul_candidate_package.v1",
                "candidate_id": "cand1",
                "status": "held",
                "reason": "thin evidence",
                "target_path": "soul.md",
            }
        ),
        encoding="utf-8",
    )

    (mem / "control" / "auto_dream_state.json").write_text(
        json.dumps(
            {
                "history": [
                    {
                        "version": 3,
                        "timestamp": "2026-06-04T09:00:00+00:00",
                        "facts_before": 12,
                        "facts_after": 9,
                        "strategy": "llm+md",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    (mem / "control" / "lifecycle.json").write_text(
        json.dumps(
            [
                {
                    "id": "mem_old",
                    "content": "x",
                    "status": "superseded",
                    "version": 1,
                    "parent_id": None,
                    "supersedes": [],
                    "superseded_by": None,
                    "expires_at": None,
                    "access_count": 0,
                    "last_accessed": None,
                    "metadata": {},
                    "created_at": "2026-06-01T00:00:00+00:00",
                    "updated_at": "2026-06-01T00:00:00+00:00",
                },
            ]
        ),
        encoding="utf-8",
    )
    return root


def _write_t2_segment_manifest(root: Path, *, session_id: str = "sess-1", segment_id: str = "seg-1") -> Path:
    manifest = root / "memory" / "t2" / "sessions" / session_id / "segments" / segment_id / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "schema": "memory_t2_segment_package.v1",
                "session_id": session_id,
                "segment_id": segment_id,
                "status": "reviewed",
            }
        ),
        encoding="utf-8",
    )
    return manifest


def _age_t3_files(root: Path, *, hours: float) -> None:
    for rel in ("self/self.md", "profiles/owner.md"):
        path = root / "memory" / rel
        if path.exists():
            _age(path, hours=hours)
    for subdir in ("knowledge", "milestones"):
        directory = root / "memory" / subdir
        if directory.exists():
            for path in directory.glob("*.md"):
                _age(path, hours=hours)


def test_overview_is_structured(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    overview = build_knowledge_overview(tmp_path, AGENT)

    assert overview["identity"]["sections"] == 2
    assert overview["identity"]["pendingSoulCandidates"] == 1
    assert overview["memory"]["active"] == 5  # 3 profile entries + 2 pages (two-plane read model)
    assert overview["memory"]["superseded"] == 1
    assert overview["memory"]["sensitiveSuppressed"] == 0  # PL2 is not PL3/PL4
    assert overview["distillers"]["dream"]["state"] == "active"
    assert overview["distillers"]["extractor"]["state"] == "never_ran"
    assert overview["linkedCapabilities"]["skillsReferenced"] == 1
    assert overview["linkedCapabilities"]["skillCandidates"] == 1
    assert overview["linkedCapabilities"]["workflowsReferenced"] == 1


def test_pages_lists_wiki_and_scenes(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    pages = list_knowledge_pages(tmp_path, AGENT)

    ids = {page["id"] for page in pages}
    assert "knowledge/memory-control-plane" in ids
    assert "milestones/railway-deployments" in ids
    wiki = next(page for page in pages if page["kind"] == "knowledge")
    assert wiki["title"] == "Memory Control Plane"


def test_get_page_returns_markdown_and_rejects_traversal(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)

    page = get_knowledge_page(tmp_path, AGENT, "knowledge/memory-control-plane")
    assert page is not None
    assert "## Current Claim" in page["markdown"]
    assert page["frontmatter"]["title"] == "Memory Control Plane"

    assert get_knowledge_page(tmp_path, AGENT, "wiki/../../soul") is None
    assert get_knowledge_page(tmp_path, AGENT, "etc/passwd") is None
    assert get_knowledge_page(tmp_path, AGENT, "wiki/NoSuchPage") is None


def test_entries_expose_heat_telemetry_and_candidates(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    entries = list_knowledge_entries(tmp_path, AGENT)

    assert len(entries) == 5  # profile entries + knowledge/milestone pages
    by_id = {entry["id"]: entry for entry in entries}
    # heat telemetry retired with the flat-T3 sidecar; two-plane rows are
    # structure-first (load column distinguishes resident vs retrieval)
    assert by_id["mem_s1"]["load"] == "P0 resident"
    assert "skill候选" in by_id["mem_s1"]["content"]
    assert by_id["mem_f1"]["file"].endswith("profiles/owner.md")


def test_entries_suppress_pl3_for_unauthorized_principal(tmp_path: Path) -> None:
    from app.services.principal_context import Principal, PrincipalRole, PrincipalStack

    _seed_workspace(tmp_path)
    mem_dir = tmp_path / str(AGENT) / "memory"
    with (mem_dir / "profiles" / "owner.md").open("a", encoding="utf-8") as fh:
        fh.write(
            '<t3_user_memory id="mem_pl3" status="active" created_at="2026-06-05" sensitivity="PL3_sensitive">'
            "<claim>salary planning is confidential</claim>"
            "<evidence><source_ref>session:salary-planning</source_ref></evidence>"
            "</t3_user_memory>\n"
        )
    owner = Principal(role=PrincipalRole.OWNER, id="owner-1")
    viewer = Principal(role=PrincipalRole.CURRENT_USER, id="viewer-1")
    stack = PrincipalStack(direct_owner=owner, current_user=viewer)

    entries = list_knowledge_entries(tmp_path, AGENT, principal_stack=stack)

    ids = {entry["id"] for entry in entries}
    assert "mem_pl3" not in ids
    # Two-plane responsibility split: sensitive claims are refused at the WRITE
    # gate and excluded from the resident prompt block; the read model lists
    # file structure and adds no second visibility filter.


def test_page_markdown_redacts_pl3_for_unauthorized_principal(tmp_path: Path) -> None:
    from app.services.principal_context import Principal, PrincipalRole, PrincipalStack

    _seed_workspace(tmp_path)
    page_path = tmp_path / str(AGENT) / "memory" / "knowledge" / "salary-planning.md"
    page_path.write_text(
        "---\ntitle: Salary Planning\ntype: concept\nstatus: active\n---\n\n"
        "## Current Claim\n\nsalary planning is confidential\n",
        encoding="utf-8",
    )
    owner = Principal(role=PrincipalRole.OWNER, id="owner-1")
    viewer = Principal(role=PrincipalRole.CURRENT_USER, id="viewer-1")
    stack = PrincipalStack(direct_owner=owner, current_user=viewer)

    page = get_knowledge_page(tmp_path, AGENT, "knowledge/salary-planning", principal_stack=stack)

    assert page is not None
    assert page["markdown"] == "[REDACTED_PL3]"
    assert "salary planning is confidential" not in page["markdown"]


def test_events_merge_audit_and_dream_history(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    events = list_knowledge_events(tmp_path, AGENT)

    kinds = {event["kind"] for event in events}
    assert "curation:scene_curation" in kinds
    assert "curation:wiki_apply" in kinds
    assert "dream:consolidation" in kinds
    # Newest first.
    assert events[0]["at"] >= events[-1]["at"]


def test_candidates_cover_all_lanes(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    candidates = list_knowledge_candidates(tmp_path, AGENT)

    assert len(candidates["skillCandidates"]) == 1
    assert len(candidates["workflowCandidates"]) == 1
    assert len(candidates["soulCandidates"]) == 1
    assert candidates["soulCandidates"][0]["candidateId"] == "cand1"
    assert any(held["stage"] == "scene_curation" for held in candidates["heldCurations"])


def test_empty_workspace_returns_empty_structures(tmp_path: Path) -> None:
    agent_id = uuid.uuid4()
    overview = build_knowledge_overview(tmp_path, agent_id)
    assert overview["memory"]["active"] == 0
    assert list_knowledge_pages(tmp_path, agent_id) == []
    assert list_knowledge_events(tmp_path, agent_id) == []
    assert list_knowledge_candidates(tmp_path, agent_id)["skillCandidates"] == []


def test_knowledge_router_registers_six_get_routes() -> None:
    from app.api.agent_knowledge import router

    paths = {route.path for route in router.routes}
    assert paths == {
        "/agents/{agent_id}/knowledge/overview",
        "/agents/{agent_id}/knowledge/pages",
        "/agents/{agent_id}/knowledge/pages/{page_id:path}",
        "/agents/{agent_id}/knowledge/entries",
        "/agents/{agent_id}/knowledge/events",
        "/agents/{agent_id}/knowledge/candidates",
    }


# ── Distiller freshness (closure plan A1: exists ≠ fresh) ──
#
# ``stale`` = the pipeline's newest INPUT is more than its stale window ahead
# of the state file: input keeps arriving but the pipeline is not keeping up.
# An idle agent (no new input since the state file) is never stale — a lying
# "stale" is the same defect as the lying "active" this fixes.


def _age(path: Path, *, hours: float) -> None:
    old = datetime.now(UTC).timestamp() - hours * 3600
    os.utime(path, (old, old))


def test_t3_consolidator_stale_when_t2_packages_outrun_t3(tmp_path: Path) -> None:
    root = _seed_workspace(tmp_path)
    _write_t2_segment_manifest(root)  # fresh reviewed T2 input for T3 consolidation
    _age_t3_files(root, hours=30)  # safely beyond the default stale window

    overview = build_knowledge_overview(tmp_path, AGENT)
    assert overview["distillers"]["heartbeat"]["state"] == "stale"


def test_t3_consolidator_active_when_t2_input_is_idle(tmp_path: Path) -> None:
    root = _seed_workspace(tmp_path)
    t2_manifest = _write_t2_segment_manifest(root)
    _age(t2_manifest, hours=200)
    _age_t3_files(root, hours=100)  # old, but newer than the last T2 input

    overview = build_knowledge_overview(tmp_path, AGENT)
    assert overview["distillers"]["heartbeat"]["state"] == "active"


def test_extractor_active_when_t2_keeps_up_with_t0_session_ledger(tmp_path: Path) -> None:
    root = _seed_workspace(tmp_path)
    source = root / "memory" / "t0" / "sessions" / "sess-1" / "segments" / "seg-1" / "source.md"
    source.parent.mkdir(parents=True)
    source.write_text("# T0 Session Ledger\n\n<t0_event>old activity</t0_event>\n", encoding="utf-8")
    _age(source, hours=30)
    _write_t2_segment_manifest(root)  # fresh enough output after the T0 input

    overview = build_knowledge_overview(tmp_path, AGENT)
    assert overview["distillers"]["extractor"]["state"] == "active"


def test_extractor_stale_when_t0_session_ledger_unprocessed(tmp_path: Path) -> None:
    root = _seed_workspace(tmp_path)
    source = root / "memory" / "t0" / "sessions" / "sess-1" / "segments" / "seg-1" / "source.md"
    source.parent.mkdir(parents=True)
    source.write_text("# T0 Session Ledger\n\n<t0_event>fresh activity</t0_event>\n", encoding="utf-8")
    t2_manifest = _write_t2_segment_manifest(root)
    _age(t2_manifest, hours=30)  # > 24h grace behind fresh ledger source

    overview = build_knowledge_overview(tmp_path, AGENT)
    assert overview["distillers"]["extractor"]["state"] == "stale"


def test_dream_stale_when_t3_outruns_state(tmp_path: Path) -> None:
    root = _seed_workspace(tmp_path)
    # Canonical memory/t3/*.md files are written fresh by _seed_workspace (T3 input);
    # age the dream state past 3 × 24h full-dream cadence.
    _age(root / "memory" / "control" / "auto_dream_state.json", hours=80)

    overview = build_knowledge_overview(tmp_path, AGENT)
    assert overview["distillers"]["dream"]["state"] == "stale"


def test_stale_threshold_follows_settings(tmp_path: Path, monkeypatch) -> None:
    from app.config import get_settings

    root = _seed_workspace(tmp_path)
    _write_t2_segment_manifest(root)
    _age_t3_files(root, hours=7)  # stale under the default 2h cadence (window 6h)...

    monkeypatch.setattr(get_settings(), "HEARTBEAT_DEFAULT_INTERVAL_MINUTES", 240)  # window now 12h
    overview = build_knowledge_overview(tmp_path, AGENT)
    assert overview["distillers"]["heartbeat"]["state"] == "active"
