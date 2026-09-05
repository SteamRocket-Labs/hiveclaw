from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "acceptance" / "weekend_production_journeys.v1.json"
GATE_PATH = ROOT / "backend" / "scripts" / "weekend_rc_gate.py"

# Digest of exactly the frozen subset {journey_families, scoring, profiles}
# (profiles pins each role's allowed_effects and cleanup boundaries). This
# pinned value is the digest of the PDEC-013-amended content (2026-09-05):
# that owner-approved amendment deliberately updated the frozen role-contract
# content — P15-ADMIN/P15-OPERATOR/P29-CADMIN/P29-PADMIN/P29-OPER inputs and
# acceptance plus the cross_role profile principal — to the owner's three-role
# contract (scoped administrator business authority; operator is a technical
# inspector view, not a fourth product identity) while preserving all 96 IDs,
# order, count and scoring. For provenance, the same-caliber digest of the
# pre-amendment base commit 0ce51f049e03c689a440075a5de8a7a9d99c609c is
# b9fc6e3b6638f3ff49f679ac0a3d70dd671fa7d4e0b4ce470f651c97362e80bd, and the
# PDEC-012 amendment before it was metadata-only. Everything outside that
# subset — contract_amended_on, execution_contract, defaults and all of
# runtime_bindings included — is deliberately not covered by this digest;
# those fields are guarded by the weekend_rc_gate constants and the tests
# below instead. Any further owner-approved amendment to frozen journey,
# scoring or profile content must update this pinned digest with review; it
# must not be refreshed automatically from the current file.
FROZEN_CONTRACT_SHA256 = "81d22fb0d6212b11d715c681260260e002fc51dbe43d7754fc9a48df69318cab"


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

    contract = manifest["execution_contract"]
    assert manifest["contract_amended_on"] == "2026-09-05"
    assert "PDEC-012 supersedes the previous single-Codex and external-agent prohibition" in contract["executor"]
    assert "zCode implements backend and functional code" in contract["executor"]
    assert "Kimi Code implements frontend UI" in contract["executor"]
    assert "Claude Code reviews first, then primary Codex independently inspects" in contract["executor"]
    assert (
        "Only at major milestones, Codex and Claude Code additionally perform "
        "reciprocal adversarial review of the plan and evidence" in contract["executor"]
    )
    assert "reconcile findings and reach an evidence-backed conclusion before advancing" in contract["executor"]
    assert "this extra exchange is not required for every small change" in contract["executor"]
    assert "workers and reviewers cannot commit, push, deploy or perform production effects" in contract["executor"]
    assert "selected runtime LLM owns task reasoning" in contract["model_agency_policy"]
    assert "primary Codex owns acceptance decomposition" in contract["model_agency_policy"]
    assert "they cannot grant authority or final production acceptance" in contract["model_agency_policy"]
    assert "First prove Agent intelligence and self-evolution" in contract["proof_order_policy"]
    assert (
        "then perform the exhaustive role-permission, RLS and adversarial-security pass"
        in contract["proof_order_policy"]
    )
    proof_orders = {family["candidate_id"]: family["proof_order"] for family in manifest["journey_families"]}
    assert {candidate_id for candidate_id, order in proof_orders.items() if order == 1} == {
        "PJ-01",
        "PJ-05",
        "PJ-06",
        "PJ-07",
        "PJ-08",
        "PJ-09",
        "PJ-33",
    }
    assert {candidate_id for candidate_id, order in proof_orders.items() if order == 3} == {
        "PJ-15",
        "PJ-16",
        "PJ-29",
        "PJ-34",
    }
    assert {candidate_id for candidate_id, order in proof_orders.items() if order == 4} == {"PJ-35"}
    assert set(proof_orders.values()) == {1, 2, 3, 4}
    assert "supported authenticated product or control-plane paths" in contract["fixture_policy"]
    assert "direct tenant or role database mutation" in contract["fixture_policy"]
    assert "No artificial Goal-wide timeout" in contract["timeout_policy"]
    assert "creates no semantic verdict" in contract["timeout_policy"]
    assert (
        "not a Hive defect, BLOCKED_PRECONDITION, provider success or semantic Closed"
        in (contract["external_readiness_policy"])
    )
    assert "remains unclosed" in contract["external_readiness_policy"]
    assert "exact Goal-created and registered synthetic targets" in contract["cleanup_policy"]
    assert "Pre-existing fixtures" in contract["cleanup_policy"]
    assert "Registered read-only negative probes" in contract["hard_stop_policy"]
    assert "without protected bytes or effects" in contract["hard_stop_policy"]
    assert "Owner instruction cannot convert unauthorized access" in contract["hard_stop_policy"]
    assert "stop that lane immediately" in contract["hard_stop_policy"]
    assert "not a Journey completion state" in manifest["scoring"]["blocked_precondition_scope"]
    assert (
        "zCode, Kimi Code and Claude Code do not perform production fixture effects"
        in (manifest["runtime_bindings"]["fixture_catalog"])
    )
    for profile in manifest["profiles"].values():
        assert "Goal-created" in " ".join(profile["cleanup"])


def _frozen_contract_digest(manifest: dict) -> str:
    frozen_subset = {
        "journey_families": manifest["journey_families"],
        "scoring": manifest["scoring"],
        "profiles": manifest["profiles"],
    }
    canonical = json.dumps(frozen_subset, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_frozen_journey_and_scoring_content_matches_pinned_digest() -> None:
    manifest = GATE.load_manifest(MANIFEST)

    assert _frozen_contract_digest(manifest) == FROZEN_CONTRACT_SHA256, (
        "frozen journey_families/scoring/profiles content changed while IDs and counts "
        "stayed intact; an owner-approved amendment updates FROZEN_CONTRACT_SHA256 "
        "with review instead of refreshing it from the current file"
    )


def test_frozen_digest_rejects_widened_profile_allowed_effects() -> None:
    manifest = GATE.load_manifest(MANIFEST)
    widened = json.loads(json.dumps(manifest))
    widened["profiles"]["employee_session"]["allowed_effects"].append("send arbitrary external email")

    assert _frozen_contract_digest(widened) != FROZEN_CONTRACT_SHA256, (
        "profiles.employee_session.allowed_effects widened while the pinned digest "
        "stayed intact; widening requires an owner-approved digest amendment"
    )


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
