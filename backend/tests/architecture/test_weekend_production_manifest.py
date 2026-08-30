from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "acceptance" / "weekend_production_journeys.v1.json"
GATE_PATH = ROOT / "backend" / "scripts" / "weekend_rc_gate.py"


def _load_gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("weekend_rc_gate", GATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GATE = _load_gate()


def _frontmatter(values: dict[str, str]) -> str:
    body = "\n".join(f"{key}: {value}" for key, value in values.items())
    return f"---\n{body}\n---\n\n# Mechanical fixture\n"


def test_production_manifest_is_frozen_complete_and_secret_free() -> None:
    manifest = GATE.load_manifest(MANIFEST)

    assert GATE.validate_manifest(manifest, manifest_path=MANIFEST) == []
    journeys = GATE.expand_manifest(manifest)
    assert len(journeys) == manifest["frozen_denominator"] == 96
    assert manifest["external_fakes_allowed"] is False
    assert {journey["candidate_id"] for journey in journeys} == set(GATE.EXPECTED_CANDIDATES)
    assert [journey["id"] for journey in journeys if journey["candidate_id"] == "PJ-03"] == (GATE.EXPECTED_COMMAND_IDS)


def test_production_manifest_freezes_formats_models_and_non_semantic_gate() -> None:
    manifest = GATE.load_manifest(MANIFEST)
    journeys = GATE.expand_manifest(manifest)

    assert {journey["id"] for journey in journeys if journey["candidate_id"] == "PJ-10"} >= (
        GATE.EXPECTED_PERSONAL_FORMAT_IDS
    )
    assert {journey["id"] for journey in journeys if journey["candidate_id"] == "PJ-30"} == (
        GATE.EXPECTED_ARTIFACT_FORMAT_IDS
    )
    assert {journey["model_label"] for journey in journeys if journey["candidate_id"] == "PJ-33"} == (
        GATE.EXPECTED_MODEL_LABELS
    )

    result = GATE.score_evidence(
        manifest,
        manifest_path=MANIFEST,
        evidence_root=ROOT / "docs" / "acceptance" / "2026-08-30-weekend-rc" / "evidence",
        deployed_commit="a" * 40,
    )
    assert result["mechanical_ready"] is False
    assert result["semantic_verdict"] == "not_computed_by_tool"
    assert result["closed"] == 0
    assert result["nptcr_percent"] == 0.0


def test_mechanical_score_requires_every_journey_and_release_fact(tmp_path: Path) -> None:
    manifest = GATE.load_manifest(MANIFEST)
    journeys = GATE.expand_manifest(manifest)
    deployed_commit = "b" * 40
    digest = GATE.manifest_sha256(MANIFEST)
    release_dir = tmp_path / deployed_commit
    release_dir.mkdir()

    for journey in journeys:
        for pass_number in (1, 2):
            metadata = {
                "journey_id": journey["id"],
                "pass": str(pass_number),
                "environment": "production",
                "source_commit": deployed_commit,
                "deployed_commit": deployed_commit,
                "manifest_sha256": digest,
                "result": "PASS",
            }
            if pass_number == 2:
                metadata.update(
                    {
                        "fault_recovery_result": "PASS",
                        "negative_authority_result": "PASS",
                        "cleanup_result": "PASS",
                    }
                )
            (release_dir / f"{journey['id']}-pass-{pass_number}.md").write_text(
                _frontmatter(metadata), encoding="utf-8"
            )

    release_metadata = {
        "result": "PASS",
        "source_commit": deployed_commit,
        "deployed_commit": deployed_commit,
        "manifest_sha256": digest,
        "backend_commit": deployed_commit,
        "backend_api_commit": deployed_commit,
        "frontend_commit": deployed_commit,
        "backend_status": "SUCCESS",
        "backend_api_status": "SUCCESS",
        "frontend_status": "SUCCESS",
        "zero_known_defects": "PASS",
        "cleanup_result": "PASS",
        "guardrail_single_agent": "PASS",
        "guardrail_growth": "PASS",
        "guardrail_benchmark": "PASS",
        "guardrail_governance": "PASS",
        "guardrail_experience": "PASS",
        "coverage_live_wiring": "PASS",
        "coverage_automation_real_postgres": "PASS",
        "coverage_signed_in_double_pass": "PASS",
        "coverage_fault_recovery": "PASS",
        "coverage_negative_authority": "PASS",
    }
    (release_dir / "release-gates.md").write_text(_frontmatter(release_metadata), encoding="utf-8")

    result = GATE.score_evidence(
        manifest,
        manifest_path=MANIFEST,
        evidence_root=tmp_path,
        deployed_commit=deployed_commit,
    )

    assert result["mechanical_ready"] is True
    assert result["semantic_verdict"] == "not_computed_by_tool"
    assert result["denominator"] == result["closed"] == 96
    assert result["nptcr_percent"] == 100.0
    assert result["evidence_coverage_percent"] == 100
    assert result["manifest_or_release_errors"] == []
    assert result["journey_errors"] == {}
