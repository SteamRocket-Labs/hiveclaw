"""Knowledge read model tests (docs/agent-memory-md-first-spec.md §12 P7).

Acceptance:
- Frontend no longer parses raw file layout for primary view.
- Overview, entries, timeline, and candidates are structured.
- Raw Markdown remains available as advanced view (workspace file APIs —
  untouched here).
"""

from __future__ import annotations

import json
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
    (mem / "wiki").mkdir(parents=True)
    (mem / "scenes").mkdir(parents=True)
    (root / "evolution").mkdir(parents=True)
    (root / "skills" / "market-research").mkdir(parents=True)

    (root / "soul.md").write_text("# Soul\n\n## Identity\n\nagent\n\n## Learned Behaviors\n\n- x\n", encoding="utf-8")
    (root / "skills" / "market-research" / "SKILL.md").write_text("---\nname: market-research\n---\n", encoding="utf-8")

    recent = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    (mem / "strategies.md").write_text(
        "# Strategies\n\n"
        f"- [2026-06-01][entry_id=mem_s1][access_count=5][last_accessed={recent}][container=skill_candidate] "
        "research → design → verify workflow\n"
        "- [2026-06-02][entry_id=mem_s2][container=workflow_candidate] nightly digest pipeline with durable state\n",
        encoding="utf-8",
    )
    (mem / "feedback.md").write_text(
        "# Feedback\n\n- [2026-06-03][entry_id=mem_f1][sensitivity=PL2_pii] user email is <Email_1>\n",
        encoding="utf-8",
    )

    (mem / "wiki" / "memory-control-plane.md").write_text(
        "---\ntitle: Memory Control Plane\ntype: concept\ntags: [memory]\nstatus: active\n---\n\n"
        "## Current Claim\n\nx\n",
        encoding="utf-8",
    )
    (mem / "scenes" / "railway-deployments.md").write_text(
        "---\ntitle: Railway Deployments\ntype: scene\nstatus: active\n---\n\n## Narrative\n\nx\n",
        encoding="utf-8",
    )

    audit_rows = [
        {"at": "2026-06-04T10:00:00+00:00", "stage": "scene_curation", "outcome": "held", "reason": "no LLM"},
        {"at": "2026-06-04T11:00:00+00:00", "stage": "wiki_apply", "outcome": "applied", "reason": "ok"},
    ]
    (mem / "distillation_audit.jsonl").write_text(
        "\n".join(json.dumps(row) for row in audit_rows) + "\n", encoding="utf-8"
    )

    (mem / "auto_dream_state.json").write_text(
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

    ledger_rows = [
        {"event": "memory_promotion_candidate", "candidate_id": "cand1", "target_type": "memory:soul"},
        {"event": "memory_promotion_decision", "candidate_id": "cand1", "decision": "hold", "reason": "thin evidence"},
    ]
    (root / "evolution" / "evolution_ledger.jsonl").write_text(
        "\n".join(json.dumps(row) for row in ledger_rows) + "\n", encoding="utf-8"
    )

    (mem / "lifecycle.json").write_text(
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


def test_overview_is_structured(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    overview = build_knowledge_overview(tmp_path, AGENT)

    assert overview["identity"]["sections"] == 2
    assert overview["identity"]["pendingSoulCandidates"] == 1
    assert overview["memory"]["active"] == 3
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
    assert "wiki/memory-control-plane" in ids
    assert "scenes/railway-deployments" in ids
    wiki = next(page for page in pages if page["kind"] == "wiki")
    assert wiki["title"] == "Memory Control Plane"


def test_get_page_returns_markdown_and_rejects_traversal(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)

    page = get_knowledge_page(tmp_path, AGENT, "wiki/memory-control-plane")
    assert page is not None
    assert "## Current Claim" in page["markdown"]
    assert page["frontmatter"]["title"] == "Memory Control Plane"

    assert get_knowledge_page(tmp_path, AGENT, "wiki/../../soul") is None
    assert get_knowledge_page(tmp_path, AGENT, "etc/passwd") is None
    assert get_knowledge_page(tmp_path, AGENT, "wiki/NoSuchPage") is None


def test_entries_expose_heat_telemetry_and_candidates(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    entries = list_knowledge_entries(tmp_path, AGENT)

    assert len(entries) == 3
    # Heat-ordered: the recalled entry first.
    assert entries[0]["id"] == "mem_s1"
    assert entries[0]["recallCount"] == 5
    assert entries[0]["containerCandidate"] == "skill_candidate"
    by_id = {entry["id"]: entry for entry in entries}
    assert by_id["mem_f1"]["sensitivity"] == "PL2_pii"


def test_entries_suppress_pl3_for_unauthorized_principal(tmp_path: Path) -> None:
    from app.services.principal_context import Principal, PrincipalRole, PrincipalStack

    _seed_workspace(tmp_path)
    mem_dir = tmp_path / str(AGENT) / "memory"
    with (mem_dir / "knowledge.md").open("a", encoding="utf-8") as fh:
        fh.write("- [2026-06-05][entry_id=mem_pl3][sensitivity=PL3_sensitive] salary planning is confidential\n")
    owner = Principal(role=PrincipalRole.OWNER, id="owner-1")
    viewer = Principal(role=PrincipalRole.CURRENT_USER, id="viewer-1")
    stack = PrincipalStack(direct_owner=owner, current_user=viewer)

    entries = list_knowledge_entries(tmp_path, AGENT, principal_stack=stack)

    ids = {entry["id"] for entry in entries}
    assert "mem_pl3" not in ids
    assert all("salary planning" not in entry["content"] for entry in entries)


def test_page_markdown_redacts_pl3_for_unauthorized_principal(tmp_path: Path) -> None:
    from app.services.principal_context import Principal, PrincipalRole, PrincipalStack

    _seed_workspace(tmp_path)
    page_path = tmp_path / str(AGENT) / "memory" / "wiki" / "salary-planning.md"
    page_path.write_text(
        "---\ntitle: Salary Planning\ntype: concept\nstatus: active\n---\n\n"
        "## Current Claim\n\nsalary planning is confidential\n",
        encoding="utf-8",
    )
    owner = Principal(role=PrincipalRole.OWNER, id="owner-1")
    viewer = Principal(role=PrincipalRole.CURRENT_USER, id="viewer-1")
    stack = PrincipalStack(direct_owner=owner, current_user=viewer)

    page = get_knowledge_page(tmp_path, AGENT, "wiki/salary-planning", principal_stack=stack)

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
