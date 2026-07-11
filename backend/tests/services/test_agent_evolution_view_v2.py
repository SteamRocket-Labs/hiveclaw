from __future__ import annotations

import json
from pathlib import Path


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def test_agent_evolution_view_v2_uses_unified_memory_and_skill_paths(tmp_path: Path) -> None:
    from app.services.agent_evolution_view import build_agent_evolution_view

    memory = tmp_path / "memory"
    (memory / "self").mkdir(parents=True)
    (memory / "profiles").mkdir(parents=True)
    (memory / "knowledge").mkdir(parents=True)
    (memory / "self" / "self.md").write_text(
        "## Capabilities\n\n### Incident response\n<!-- id: self-incident-response -->\n"
        "Recurring launch checklist [container=skill_candidate].\n",
        encoding="utf-8",
    )
    (memory / "profiles" / "owner.md").write_text(
        "## Owner Profile\n\n### Cadence\n<!-- id: owner-cadence -->\nDaily status summaries.\n",
        encoding="utf-8",
    )
    (memory / "knowledge" / "launch-checklist.md").write_text(
        "---\ntitle: Launch Checklist\nstatus: active\n---\n\n## Current Claim\nUse the recurring launch checklist.\n",
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
            "source_refs": ["memory/self/self.md"],
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
                    "state": "provisional",
                    "active_version_hash": "sha256:active",
                    "last_candidate_id": "cand-patch",
                    "metadata": {"trial_path": "evolution/skill_trials/cand-patch/trial.json"},
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
        evolution / "skill_trials" / "cand-patch" / "trial.json",
        {
            "schema": "skill_provisional_trial.v1",
            "candidate_id": "cand-patch",
            "state": "provisional",
            "started_at": "2026-07-11T00:00:00Z",
            "updated_at": "2026-07-11T00:03:00Z",
            "window_days": 14,
            "thresholds": {"positive": 3, "negative": 2},
            "signals": {"positive": [{"id": "p1"}, {"id": "p2"}], "negative": [{"id": "n1"}]},
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
            "source_refs": ["evolution/skill_usage.jsonl", "memory/knowledge/launch-checklist.md"],
        },
    )
    (evolution / "scorecard.md").write_text("# legacy\n", encoding="utf-8")

    view = build_agent_evolution_view(tmp_path)

    assert view["schema"] == "agent_evolution_view.v2"
    assert {event["lane"] for event in view["timeline"]} >= {"memory", "soul", "skill_tuning"}
    assert view["lanes"]["memory"][0]["stage"] == "t3_job"
    assert view["lanes"]["memory"][0]["source_refs"] == ["memory/t2/sessions/s1/segments/seg1/package.md"]
    assert view["lanes"]["soul"][0]["stage"] == "soul_candidate"
    assert view["lanes"]["skill_tuning"][0]["stage"] == "skill_candidate"
    assert (
        view["path_contract"]["t2_segment_packages"]
        == "memory/t2/sessions/<session_id>/segments/<segment_id>/{summary,labels,review,manifest}"
    )
    assert view["path_contract"]["t3_profile_self"] == "memory/self/self.md"
    assert view["path_contract"]["t3_profile_owner"] == "memory/profiles/owner.md"
    assert view["path_contract"]["t3_knowledge_pages"] == "memory/knowledge/<slug>.md"
    assert "t3_capabilities" not in view["path_contract"]
    assert view["path_contract"]["skill_registry"] == "evolution/skill_registry.json"
    assert view["memory_learning"]["pending_t3_jobs"][0]["job_id"] == "job-1"
    assert view["memory_learning"]["t3_targets"]["self"]["line_count"] > 0
    assert view["memory_learning"]["t3_targets"]["knowledge_pages"]["line_count"] > 0
    assert view["soul"]["pending_candidates"][0]["candidate_id"] == "soul-1"
    assert view["skill_ecosystem"]["summary"]["by_origin"]["user_skill_creator"] == 1
    assert view["skill_ecosystem"]["summary"]["by_origin"]["system_builtin"] == 1
    assert view["skill_ecosystem"]["summary"]["provisional"] == 1
    trial_skill = next(item for item in view["skill_ecosystem"]["skills"] if item["skill_name"] == "incident-response")
    assert trial_skill["trial"] == {
        "state": "provisional",
        "positive_count": 2,
        "positive_threshold": 3,
        "negative_count": 1,
        "negative_threshold": 2,
        "window_days": 14,
        "started_at": "2026-07-11T00:00:00Z",
        "updated_at": "2026-07-11T00:03:00Z",
    }
    assert view["skill_ecosystem"]["skills"][0]["skill_name"] == "web-search"
    assert view["skill_ecosystem"]["skills"][0]["evolvable"] is False
    assert view["skill_tuning"]["candidates"][0]["candidate_id"] == "cand-patch"
    assert view["legacy_audit"]["detected_legacy_files"] == ["evolution/scorecard.md"]
