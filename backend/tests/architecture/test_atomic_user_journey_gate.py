from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "acceptance" / "atomic_user_journeys.v1.json"
SPEC = ROOT / "frontend" / "e2e" / "atomic-user-journeys.spec.ts"
CONFIG = ROOT / "frontend" / "playwright.journeys.config.ts"
DEFAULT_CONFIG = ROOT / "frontend" / "playwright.config.ts"
PACKAGE = ROOT / "frontend" / "package.json"
CI = ROOT / ".github" / "workflows" / "harness-ci.yml"


def test_release_manifest_has_all_fifteen_atomic_journeys() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["schema"] == "hive.atomic_user_journeys.v1"
    journeys = manifest["journeys"]
    assert [journey["id"] for journey in journeys] == [f"J-{index:02d}" for index in range(1, 16)]
    for journey in journeys:
        assert set(journey["atoms"]) == {
            "input",
            "authority",
            "execution",
            "evidence",
            "recovery",
            "consumption",
            "acceptance",
        }
        assert journey["product_endpoints"]
        assert all(
            endpoint.startswith(("GET ", "POST ", "PUT ", "DELETE ")) for endpoint in journey["product_endpoints"]
        )
        assert journey["browser_assertions"]
        assert set(journey["faults"]) >= {"permission_denial", "duplicate_or_out_of_order"}

    endpoint_matrix = {journey["id"]: journey["product_endpoints"] for journey in journeys}
    assert endpoint_matrix["J-02"] == [
        "POST /api/chat/upload",
        "GET /api/agents/{agent_id}/files/?path=workspace/uploads",
        "GET /api/agents/{agent_id}/files/download?path={workspace_path}",
    ]
    assert endpoint_matrix["J-04"] == [
        "POST /api/agents/{agent_id}/sessions/{session_id}/goals",
        "GET /api/agents/{agent_id}/sessions/{session_id}/workbench?operator_view=true",
        "GET /api/agents/{agent_id}/sessions/{session_id}/transcript?schema_version=2",
    ]
    assert endpoint_matrix["J-06"] == [
        "POST /api/agents/{agent_id}/sessions/{session_id}/branches",
        "GET /api/agents/{agent_id}/sessions/{branch_session_id}/lineage",
    ]
    assert endpoint_matrix["J-10"] == [
        "POST /api/agents/{agent_id}/agent-teams",
        "POST /api/agents/{agent_id}/agent-teams/{team_id}/events",
        "POST /api/agents/{agent_id}/agent-teams/{team_id}/close",
        "GET /api/agents/{agent_id}/agent-teams/{team_id}/workbench",
    ]


def test_browser_journeys_use_real_backend_and_have_one_test_per_manifest_entry() -> None:
    source = SPEC.read_text(encoding="utf-8")
    assert "page.route(" not in source
    assert "route.fulfill(" not in source
    assert "HIVE_JOURNEY_BACKEND_URL" in source
    assert len(re.findall(r"test\(journey\.id", source)) == 1
    assert "for (const journey of JOURNEYS)" in source
    assert "expectJourneyEvidence" in source


def test_full_stack_config_starts_real_backend_and_controlled_external_fakes() -> None:
    source = CONFIG.read_text(encoding="utf-8")
    assert "tests.journeys.run_backend" in source
    assert "tests.journeys.fake_external_provider" in source
    assert "reuseExistingServer: false" in source
    assert "workers: 1" in source
    assert "trace: 'retain-on-failure'" in source
    assert "video: 'retain-on-failure'" in source


def test_default_visual_suite_does_not_collect_the_full_stack_atomic_spec() -> None:
    source = DEFAULT_CONFIG.read_text(encoding="utf-8")
    assert "testIgnore: 'atomic-user-journeys.spec.ts'" in source


def test_atomic_journeys_are_a_required_ci_release_gate() -> None:
    package = json.loads(PACKAGE.read_text(encoding="utf-8"))
    assert package["scripts"]["test:e2e:journeys"] == (
        "env -u NO_COLOR playwright test --config playwright.journeys.config.ts"
    )
    ci = CI.read_text(encoding="utf-8")
    assert "atomic-user-journeys:" in ci
    assert "postgres:15" in ci
    assert "redis:7" in ci
    assert "npm run test:e2e:journeys" in ci
    assert "if: always()" in ci
    assert "atomic-journey-mechanical-evidence" in ci
    assert "frontend/playwright-journey-report" in ci
    assert "frontend/test-results" in ci


def test_backend_harness_exposes_no_test_only_product_route() -> None:
    source = (ROOT / "backend" / "tests" / "journeys" / "run_backend.py").read_text(encoding="utf-8")
    assert "app.main:app" in source
    assert "tests.journeys.prepare_database" in source
    assert "app.scripts.grant_rls_app_role" in source
    assert "SCHEMA_DATABASE_URL" in source
    assert "include_router" not in source
    assert "/test" not in source
    assert "/journey" not in source
