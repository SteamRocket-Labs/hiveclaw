from __future__ import annotations

import json
from pathlib import Path


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def test_agent_evolution_view_v2_uses_unified_memory_and_skill_paths(tmp_path: Path) -> None:
    from app.services.agent_evolution_view import build_agent_evolution_view

    memory = tmp_path / "memory"
    (memory / "t3").mkdir(parents=True)
    (memory / "t3" / "capabilities.md").write_text(
        "- [2026-06-20] recurring launch checklist [container=skill_candidate]\n",
        encoding="utf-8",
    )
    _write_json(
        memory / ".staging" / "t3_jobs" / "job-1" / "manifest.json",
        {
            "schema": "t3_consolidation_job.v1",
            "job_id": "job-1",
            "status": "awaiting_agent_review",
            "source_refs": ["memory/t2/sessions/s1/segments/seg1/package.md"],
        },
    )
    _write_json(
        memory / ".staging" / "soul_candidates" / "soul-1" / "manifest.json",
        {
            "schema": "soul_candidate.v1",
            "candidate_id": "soul-1",
            "status": "candidate",
            "source_refs": ["memory/t3/worker.md"],
        },
    )

    evolution = tmp_path / "evolution"
    _write_json(
        evolution / "skill_registry.json",
        {
            "schema": "skill_registry.v1",
            "skills": {
                "incident-response": {
                    "skill_name": "incident-response",
                    "skill_origin": "user_skill_creator",
                    "evolvable": True,
                    "state": "active",
                    "active_version_hash": "sha256:active",
                    "last_candidate_id": "cand-patch",
                },
                "web-search": {
                    "skill_name": "web-search",
                    "skill_origin": "system_builtin",
                    "evolvable": False,
                    "state": "active",
                },
            },
        },
    )
    _write_json(
        evolution / "skill_usage.json",
        {
            "incident-response": {"state": "active", "use_count": 12, "last_used_at": "2026-06-20T10:00:00Z"},
            "web-search": {"state": "active", "use_count": 40, "last_used_at": "2026-06-20T11:00:00Z"},
        },
    )
    _write_json(
        evolution / "skill_candidates" / "cand-patch" / "manifest.json",
        {
            "schema": "skill_candidate_package.v1",
            "candidate_id": "cand-patch",
            "skill_name": "incident-response",
            "status": "patch",
            "skill_origin": "user_skill_creator",
            "evolvable": True,
            "source_refs": ["evolution/skill_usage.jsonl", "memory/t3/capabilities.md"],
        },
    )
    (evolution / "scorecard.md").write_text("# legacy\n", encoding="utf-8")

    view = build_agent_evolution_view(tmp_path)

    assert view["schema"] == "agent_evolution_view.v2"
    assert (
        view["path_contract"]["t2_segment_packages"]
        == "memory/sessions/<session_id>/segments/<segment_id>/{summary,labels,review,manifest}"
    )
    assert view["path_contract"]["t3_capabilities"] == "memory/t3/capabilities.md"
    assert view["path_contract"]["skill_registry"] == "evolution/skill_registry.json"
    assert view["memory_learning"]["pending_t3_jobs"][0]["job_id"] == "job-1"
    assert view["soul"]["pending_candidates"][0]["candidate_id"] == "soul-1"
    assert view["skill_ecosystem"]["summary"]["by_origin"]["user_skill_creator"] == 1
    assert view["skill_ecosystem"]["summary"]["by_origin"]["system_builtin"] == 1
    assert view["skill_ecosystem"]["skills"][0]["skill_name"] == "web-search"
    assert view["skill_ecosystem"]["skills"][0]["evolvable"] is False
    assert view["skill_tuning"]["candidates"][0]["candidate_id"] == "cand-patch"
    assert view["legacy_audit"]["detected_legacy_files"] == ["evolution/scorecard.md"]
